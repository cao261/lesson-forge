"""本地网页界面：贴链接 → 看进度 → 预览 → 下载 Word。"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.config import SETTINGS
from app.pipeline import JobOptions, JobResult, run_pipeline, work_dir_for

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

app = FastAPI(title="LessonForge · 视频转教案")


@dataclass
class Job:
    id: str
    url: str
    options: JobOptions
    status: str = "pending"          # pending | running | done | failed | cancelled
    progress: float = 0.0
    stage: str = ""
    message: str = "排队中…"
    logs: list[str] = field(default_factory=list)
    error: str = ""
    result: JobResult | None = None
    events: queue.Queue = field(default_factory=queue.Queue)
    created_at: float = 0.0
    cancel: bool = False

    def push(self, kind: str, payload: dict) -> None:
        self.events.put({"type": kind, **payload})


JOBS: dict[str, Job] = {}

# 常驻服务跑久了 Job 只增不减，而每个 Job 都引用着它的 result.lesson 和
# 全部日志——几十个任务就是几十份教案的内存。留最近这么多就够了。
MAX_JOBS = 30


def _evict_jobs() -> None:
    """丢掉最老的、已经结束的 Job。排队中/正在跑的绝不能动。"""
    if len(JOBS) <= MAX_JOBS:
        return
    for jid, job in sorted(JOBS.items(), key=lambda kv: kv[1].created_at or 0):
        if len(JOBS) <= MAX_JOBS:
            break
        if job.status in {"pending", "", "running"}:
            continue
        JOBS.pop(jid, None)

# 批量任务共用一个工作线程，逐个跑。
# Whisper 要占显存、模型接口有 RPM 限制，并发跑只会互相拖慢甚至把显存挤爆，
# 所以排队是唯一稳妥的选择；单任务提交时也是立刻开跑，用户感觉不到延迟。
_QUEUE: queue.Queue = queue.Queue()
_WORKER_STARTED = False


def _run(job: Job) -> None:
    if job.cancel:
        job.status = "cancelled"
        job.message = "已取消（还没开始就取消了）"
        job.push("cancelled", {"message": job.message})
        return

    job.status = "running"
    job.push("status", {"status": "running", "message": "开始处理", "progress": 0.0})

    def on_progress(frac: float, message: str) -> None:
        job.progress = max(0.0, min(1.0, frac))
        job.message = message
        job.push("progress", {"progress": job.progress, "message": message})

    def on_log(line: str) -> None:
        job.logs.append(line)
        job.push("log", {"line": line})

    try:
        result = run_pipeline(job.options, on_progress, on_log)
        job.result = result
        job.status = "done"
        job.progress = 1.0
        job.message = "完成"
        job.push("done", {
            "job_id": job.id,
            "docx": f"/api/jobs/{job.id}/download/docx",
            "docx_student": f"/api/jobs/{job.id}/download/docx?audience=student",
            "preview": f"/api/jobs/{job.id}/preview",
            "preview_student": f"/api/jobs/{job.id}/preview?audience=student",
            "title": result.lesson.meta.title,
            "stats": result.lesson.stats,
            "verdict": result.lesson.review.verdict,
            "elapsed_min": round(result.elapsed_sec / 60, 1),
            "work_dir": str(result.work_dir),
        })
    except Exception as exc:  # noqa: BLE001
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.message = job.error
        job.logs.append(traceback.format_exc())
        job.push("failed", {"error": job.error})


def _worker() -> None:
    """串行消费任务队列，永不退出。"""
    while True:
        job = _QUEUE.get()
        try:
            if job is None:
                return
            _run(job)
        finally:
            _QUEUE.task_done()


def _enqueue(job: Job) -> None:
    global _WORKER_STARTED
    if not _WORKER_STARTED:
        _WORKER_STARTED = True
        threading.Thread(target=_worker, daemon=True).start()
    _QUEUE.put(job)


def _queue_depth() -> int:
    return _QUEUE.qsize()


# ------------------------------------------------------------------ 页面
@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
def get_config() -> JSONResponse:
    providers = [
        {
            "name": p["name"],
            "model": p["model"],
            "protocol": p["protocol"],
            "style": p.get("style", ""),
            "rpm": p["rpm"],
            "vision": p["vision"],
            "configured": bool(p["api_key"]),
        }
        for p in SETTINGS.active_providers
    ]
    return JSONResponse({
        "has_key": SETTINGS.has_llm,
        "mock": SETTINGS.mock,
        "model": SETTINGS.model,
        "providers": providers,
        "whisper": SETTINGS.whisper_model,
        "workspace": str(SETTINGS.workspace_dir),
        "export_dir": str(SETTINGS.export_path),
        "mml2omml": bool(SETTINGS.mml2omml_xsl),
        "host": SETTINGS.host,
        "port": SETTINGS.port,
    })


# ------------------------------------------------------------------ 任务
def _collect_urls(payload: dict) -> list[str]:
    """从请求里收链接，兼容单条 url 和批量 urls（列表或换行分隔文本），去重保序。"""
    raw: list[str] = []
    single = (payload.get("url") or "").strip()
    if single:
        raw.append(single)
    bulk = payload.get("urls")
    if isinstance(bulk, str):
        raw.extend(bulk.replace("\r", "\n").replace("，", "\n").split("\n"))
    elif isinstance(bulk, list):
        raw.extend(str(x) for x in bulk)

    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        item = item.strip().strip('"').strip("'")
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _make_options(url: str, payload: dict) -> JobOptions:
    return JobOptions(
        url=url,
        subject=(payload.get("subject") or "").strip(),
        grade=(payload.get("grade") or "").strip(),
        duration_min=float(payload.get("duration_min") or 90),
        reuse=bool(payload.get("reuse", True)),
        do_review=bool(payload.get("do_review", True)),
        max_review_rounds=payload.get("max_review_rounds"),
    )


def _validate(url: str) -> None:
    if url.startswith(("http://", "https://")):
        return
    # 本地视频文件也支持，省得用户还要传给别人
    if Path(url).expanduser().exists():
        return
    raise HTTPException(400, f"既不是 http(s) 链接，本地也找不到这个文件：{url}")


@app.post("/api/jobs")
async def create_job(payload: dict) -> JSONResponse:
    urls = _collect_urls(payload)
    if not urls:
        raise HTTPException(400, "请至少填写一个视频链接")
    for u in urls:
        _validate(u)

    jobs: list[Job] = []
    for u in urls:
        job = Job(id=uuid.uuid4().hex[:10], url=u, options=_make_options(u, payload),
                  created_at=time.time())
        JOBS[job.id] = job
        jobs.append(job)
    _evict_jobs()
    for job in jobs:
        _enqueue(job)

    return JSONResponse({
        "job_id": jobs[0].id,          # 单条时保持旧字段，前端不用改
        "job_ids": [j.id for j in jobs],
        "count": len(jobs),
    })


@app.get("/api/jobs")
def list_jobs() -> JSONResponse:
    order = sorted(JOBS.values(), key=lambda j: j.created_at or 0, reverse=True)
    return JSONResponse({
        "queue_depth": _queue_depth(),
        "items": [{
            "job_id": j.id,
            "url": j.url,
            "status": j.status,
            "progress": j.progress,
            "message": j.message,
            "error": j.error,
            "title": j.result.lesson.meta.title if j.result else "",
            "verdict": j.result.lesson.review.verdict if j.result else "",
            "download": f"/api/jobs/{j.id}/download/docx" if j.result else "",
        } for j in order],
    })


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status == "running":
        raise HTTPException(409, "任务正在跑，中途打断会留下半成品，请等它跑完")
    job.cancel = True
    if job.status in {"pending", ""}:
        job.status = "cancelled"
        job.message = "已取消"
    return JSONResponse({"ok": True, "status": job.status})


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> JSONResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    payload = {
        "job_id": job.id,
        "status": job.status,
        "progress": job.progress,
        "message": job.message,
        "error": job.error,
        # 心跳每 45 秒一条，长任务很快就会把 60 行的窗口冲掉，
        # 刷新页面就看不到前面的关键行了。放宽到 400 行。
        "logs": job.logs[-400:],
    }
    if job.status == "done" and job.result:
        payload["result"] = {
            "title": job.result.lesson.meta.title,
            "stats": job.result.lesson.stats,
            "verdict": job.result.lesson.review.verdict,
            "elapsed_min": round(job.result.elapsed_sec / 60, 1),
            "work_dir": str(job.result.work_dir),
            "download": f"/api/jobs/{job.id}/download/docx",
            "preview": f"/api/jobs/{job.id}/preview",
        }
    return JSONResponse(payload)


@app.get("/api/jobs/{job_id}/stream")
async def stream(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")

    async def gen():
        loop = asyncio.get_event_loop()
        yield f"data: {json.dumps({'type': 'snapshot', 'status': job.status, 'progress': job.progress, 'message': job.message}, ensure_ascii=False)}\n\n"
        while True:
            if job.status in {"done", "failed", "cancelled"}:
                try:
                    while True:
                        event = job.events.get_nowait()
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    pass
                return
            try:
                event = await loop.run_in_executor(None, job.events.get, True, 10)
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield ": keep-alive\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
    })


@app.post("/api/jobs/{job_id}/rerun")
async def rerun(job_id: str, payload: dict | None = None) -> JSONResponse:
    """复用已下载/已转写的素材，只重跑后面的环节（改完提示词调教案时用）。"""
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    payload = payload or {}
    options = JobOptions(
        url=job.url,
        subject=(payload.get("subject") or job.options.subject).strip(),
        grade=(payload.get("grade") or job.options.grade).strip(),
        duration_min=float(payload.get("duration_min") or job.options.duration_min),
        reuse=True,
        do_review=bool(payload.get("do_review", True)),
    )
    job.options = options
    job.status = "pending"
    job.progress = 0.0
    job.error = ""
    job.result = None
    job.cancel = False
    job.message = "重新生成中…"
    job.events = queue.Queue()
    _enqueue(job)
    return JSONResponse({"job_id": job.id})


# ------------------------------------------------------------------ 产物
def _result(job_id: str) -> JobResult:
    job = JOBS.get(job_id)
    if not job or job.status != "done" or not job.result:
        raise HTTPException(404, "还没有可下载的结果")
    return job.result


@app.get("/api/jobs/{job_id}/download/docx")
def download_docx(job_id: str, audience: str = "teacher") -> FileResponse:
    result = _result(job_id)
    path = result.student_docx_path if audience == "student" else result.docx_path
    if not path or not path.exists():
        raise HTTPException(404, "这份文档不存在")
    return FileResponse(
        path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=path.name,
    )


@app.get("/api/jobs/{job_id}/preview")
def preview(job_id: str, audience: str = "teacher") -> HTMLResponse:
    result = _result(job_id)
    path = result.student_html_path if audience == "student" else result.html_path
    if not path or not path.exists():
        raise HTTPException(404, "这份预览不存在")
    html = path.read_text(encoding="utf-8")
    # 让预览里的截图走同源路由
    html = html.replace('src="shots/', f'src="/api/jobs/{job_id}/shots/')
    return HTMLResponse(html)


@app.get("/api/jobs/{job_id}/shots/{name}")
def shot(job_id: str, name: str) -> FileResponse:
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    work = work_dir_for(job.url) if not job.result else job.result.work_dir
    path = (work / "shots" / Path(name).name)
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


@app.get("/api/jobs/{job_id}/json")
def lesson_json(job_id: str) -> FileResponse:
    result = _result(job_id)
    return FileResponse(result.work_dir / "lesson.json", media_type="application/json")


# ------------------------------------------------------------------ 历史
def _history_dir(folder: str) -> Path:
    """把 URL 里的 folder 段收敛成工作区内的单个子目录。

    Path(folder).name 挡掉 ".." 和路径分隔符。少了这一步，
    folder=".." 会通过 work.exists() 校验并落到仓库根目录，
    下载/预览/截图三个接口就都能读到工作区之外的文件。
    """
    return SETTINGS.workspace_dir / Path(folder).name


def _export_manifest(folder: Path) -> dict:
    """读任务目录里的成品清单（成品本体在项目外面的「教案」目录里）。"""
    try:
        data = json.loads((folder / "export.json").read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


@app.get("/api/history")
def history() -> JSONResponse:
    items = []
    for folder in sorted(SETTINGS.workspace_dir.glob("*"), reverse=True):
        lesson_file = folder / "lesson.json"
        if not lesson_file.exists():
            continue
        try:
            data = json.loads(lesson_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        manifest = _export_manifest(folder)
        teacher_docx = Path(manifest["docx"]) if manifest.get("docx") else None
        student_docx = Path(manifest["student_docx"]) if manifest.get("student_docx") else None
        has_teacher_html = next(folder.glob("*教师备课版*.html"), None) is not None
        has_student_html = next(folder.glob("*学生讲义*.html"), None) is not None

        items.append({
            "id": folder.name,
            "title": data.get("meta", {}).get("title", folder.name),
            "source_title": data.get("meta", {}).get("source_title", ""),
            "generated_at": data.get("meta", {}).get("generated_at", ""),
            "stats": data.get("stats", {}),
            "verdict": data.get("review", {}).get("verdict", ""),
            "docx": f"/api/history/{folder.name}/docx?audience=teacher" if teacher_docx else "",
            "docx_student": f"/api/history/{folder.name}/docx?audience=student" if student_docx else "",
            "preview": f"/api/history/{folder.name}/preview?audience=teacher" if has_teacher_html else "",
            "preview_student": f"/api/history/{folder.name}/preview?audience=student" if has_student_html else "",
        })
    return JSONResponse({"items": items})


@app.get("/api/history/{folder}/docx")
def history_docx(folder: str, audience: str = "teacher") -> FileResponse:
    work = _history_dir(folder)
    if not work.exists():
        raise HTTPException(404, "该记录文件夹不存在")
    # 成品已经搬到项目外面去了，本体在清单 export.json 里指的地方。
    manifest = _export_manifest(work)
    key = "student_docx" if audience == "student" else "docx"
    doc = Path(manifest[key]) if manifest.get(key) else None
    if not doc or not doc.exists():
        raise HTTPException(404, "成品文件不在了。它在项目外面的「教案」目录里，"
                                 "可能是被手动移走或删除了")
    return FileResponse(
        doc,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=doc.name,
    )


@app.get("/api/history/{folder}/preview")
def history_preview(folder: str, audience: str = "teacher") -> HTMLResponse:
    work = _history_dir(folder)
    if not work.exists():
        raise HTTPException(404, "该记录文件夹不存在")
    pattern = "*学生讲义*.html" if audience == "student" else "*教师备课版*.html"
    preview_file = next(work.glob(pattern), None)
    if not preview_file:
        htmls = sorted(work.glob("*.html"))
        if not htmls:
            raise HTTPException(404, "该记录没有 HTML 预览文件")
        preview_file = htmls[0]
    html = preview_file.read_text(encoding="utf-8")
    html = html.replace('src="shots/', f'src="/api/history/{folder}/shots/')
    return HTMLResponse(html)


@app.get("/api/history/{folder}/shots/{name}")
def history_shot(folder: str, name: str) -> FileResponse:
    work = _history_dir(folder)
    path = work / "shots" / Path(name).name
    if not path.exists():
        raise HTTPException(404, "图片不存在")
    return FileResponse(path)


app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")