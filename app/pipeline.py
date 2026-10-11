"""端到端流水线：B站链接 → 教案 Word。

每一步的产物都落在工作目录里并复用：
改提示词重跑时，只重跑受影响的后段，不用重新下载和转写。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import SETTINGS
from app.llm.client import set_notice_hook
from app.media import ffmpeg
from app.models import Cue, Lesson, LessonMeta
from app import textnorm
from app.stages import asr, compose, export, fetch, normalize, review as review_stage, segment, shots, vision

Progress = Callable[[float, str], None]
Logger = Callable[[str], None]

STAGES = [
    ("fetch", "下载视频", 0.00, 0.12),
    ("asr", "语音转写", 0.12, 0.42),
    ("segment", "知识点切分", 0.42, 0.54),
    ("shots", "例题截图", 0.54, 0.70),
    ("vision", "题目转写", 0.70, 0.84),
    ("compose", "生成教案", 0.84, 0.91),
    ("review", "审核修订", 0.91, 0.98),
    ("export", "导出 Word", 0.98, 1.00),
]


@dataclass
class JobOptions:
    url: str
    subject: str = ""
    grade: str = ""
    duration_min: float = 90.0
    max_review_rounds: int | None = None
    reuse: bool = True          # 复用已有的下载/字幕/切分结果
    do_review: bool = True


@dataclass
class JobResult:
    job_id: str
    work_dir: Path
    meta: object
    lesson: Lesson
    docx_path: Path
    html_path: Path
    student_docx_path: Path | None = None
    student_html_path: Path | None = None
    elapsed_sec: float = 0.0
    logs: list[str] = field(default_factory=list)


def work_dir_for(url: str) -> Path:
    key = hashlib.sha1(url.strip().encode("utf-8")).hexdigest()[:10]
    return SETTINGS.workspace_dir / key


_LABEL_RE = re.compile(r"[（(【\[]([^（()【】\[\]]{2,20})[）)】\]]")


def _find_ungrounded_labels(kps: list, cues: list[Cue]) -> list[str]:
    """找出标题里字幕原文没有逐字出现过的括注标签。

    **只报告，不自动删**。实测教训：这份 ASR 噪声很重的字幕里，
    "（第二类题型）" 的"第二类"出现过、"题型"被听成了"长考提醒"，
    整串匹配失败 —— 自动删会误伤真标签。三条测试错了两条。
    删不删由老师决定，这里负责把疑点指出来。
    """
    transcript = "".join(c.text for c in cues)
    if not transcript.strip():
        return []
    norm = re.sub(r"\s+", "", transcript).lower()
    found: list[str] = []
    for kp in kps:
        for owner in [kp] + list(kp.examples):
            title = getattr(owner, "title", "") or ""
            for m in _LABEL_RE.finditer(title):
                inner = re.sub(r"\s+", "", m.group(1)).lower()
                if inner and inner not in norm:
                    found.append(f"{getattr(owner, 'id', '?')}「{m.group(0)}」")
    return found


def _band(stage: str) -> tuple[float, float]:
    for name, _label, lo, hi in STAGES:
        if name == stage:
            return lo, hi
    return 0.0, 1.0


class _Reporter:
    """把各阶段内部的 0~1 进度映射到全局进度条。"""

    def __init__(self, stage: str, on_progress: Progress | None, on_log: Logger | None):
        self.stage = stage
        self.on_progress = on_progress
        self.on_log = on_log
        self.lo, self.hi = _band(stage)
        self.label = next(label for name, label, _, _ in STAGES if name == stage)

    def __call__(self, frac: float, message: str) -> None:
        if self.log:
            self.log(f"[{self.label}] {message}")
        if self.on_progress:
            self.on_progress(self.lo + (self.hi - self.lo) * max(0.0, min(1.0, frac)), message)

    @property
    def log(self) -> Logger | None:
        return self.on_log


MODE_STAMP = ".mode"


def _mode_signature() -> str:
    """当前运行模式的指纹。

    离线演示模式产出的字幕/切分/题干全是假数据，绝不能被真实模式复用；
    反过来也一样。不一致就整份缓存作废。
    """
    return "mock" if SETTINGS.mock else f"live:{SETTINGS.model}:{SETTINGS.whisper_model}"


def _cache_is_valid(work_dir: Path) -> bool:
    stamp = work_dir / MODE_STAMP
    if not stamp.exists():
        # 老版本没有标记文件，只要不是 mock 就放行，避免正常用户被迫重跑
        return not SETTINGS.mock
    try:
        return stamp.read_text(encoding="utf-8").strip() == _mode_signature()
    except OSError:
        return False


def _invalidate_cache(work_dir: Path, reason: str, log: Logger | None) -> None:
    if log:
        log(f"缓存与当前模式不一致（{reason}），重新处理")
    for name in ("transcript.json", "segments.json", "candidates.json",
                 "lesson.json", "lesson.draft.json"):
        target = work_dir / name
        if target.exists():
            target.unlink(missing_ok=True)
    meta_file = work_dir / MODE_STAMP
    try:
        meta_file.unlink(missing_ok=True)
    except OSError:
        pass
    shutil.rmtree(work_dir / "shots", ignore_errors=True)


def run_pipeline(
    options: JobOptions,
    on_progress: Progress | None = None,
    on_log: Logger | None = None,
) -> JobResult:
    t0 = time.time()
    work_dir = work_dir_for(options.url)
    work_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = work_dir / "shots"
    logs: list[str] = []

    def log(message: str) -> None:
        logs.append(message)
        if on_log:
            on_log(message)

    # 模型降级/限速事件直接进日志，网页界面能实时看到。
    # 先清空再挂：钩子是进程级全局的，不清会把上一个任务的闭包（连同它
    # 引用的 Job 和全部日志）一直钉在列表里——新任务的模型事件会写进
    # 旧任务的日志和 SSE 队列，前端会当作本任务事件渲染出来。
    set_notice_hook(None)
    set_notice_hook(log)

    log(f"工作目录：{work_dir}")

    chain = " → ".join(
        f"{p['name']}({p['model']}{'，限速 %d RPM' % p['rpm'] if p['rpm'] else ''})"
        for p in SETTINGS.active_providers
    )
    log(f"模型链路：{chain}")

    if options.reuse and not _cache_is_valid(work_dir):
        _invalidate_cache(work_dir, f"缓存标记 {_mode_signature()}", log)
    try:
        (work_dir / MODE_STAMP).write_text(_mode_signature(), encoding="utf-8")
    except OSError:
        pass

    # ---------------------------------------------------------- 1. 下载
    rep = _Reporter("fetch", on_progress, log)
    meta, video = fetch.fetch_video(options.url, work_dir, rep)
    transcript_path = work_dir / "transcript.json"

    # ---------------------------------------------------------- 2. 转写
    rep = _Reporter("asr", on_progress, log)
    cues: list = []
    if options.reuse and transcript_path.exists() and transcript_path.stat().st_size > 40:
        cues = asr.load(transcript_path)
        log(f"复用已有字幕 {len(cues)} 段（{transcript_path.name}）")
        rep(1.0, f"复用已有字幕，共 {len(cues)} 段")
    else:
        audio = work_dir / "audio.wav"
        if not audio.exists() or audio.stat().st_size < 1000:
            ffmpeg.extract_audio(video, audio, progress=lambda f, m: rep(f * 0.12, m))
        else:
            rep(0.12, "复用已有音轨")
        hint = f"《{meta.title}》。" + (meta.description[:300] if meta.description else "")
        cues = asr.transcribe(audio, meta.duration, progress=lambda f, m: rep(0.12 + 0.88 * f, m), hint=hint)
        asr.save(cues, transcript_path)

    if not cues:
        raise RuntimeError("转写结果为空，请确认视频里有语音内容。")

    # ---------------------------------------------------------- 3. 切分知识点
    rep = _Reporter("segment", on_progress, log)
    segment_path = work_dir / "segments.json"
    kps = None
    topic = ""
    subject_grade = ""
    if options.reuse and segment_path.exists():
        try:
            payload = json.loads(segment_path.read_text(encoding="utf-8"))
            from app.models import KnowledgePoint

            kps = [KnowledgePoint(**item) for item in payload["knowledge"]]
            topic = payload.get("topic", "")
            subject_grade = payload.get("subject_grade", "")
            log(f"复用已有知识点切分：{len(kps)} 个")
            rep(1.0, f"复用已有切分结果，{len(kps)} 个知识点")
        except Exception:  # noqa: BLE001
            kps = None
    if kps is None:
        topic, subject_grade, kps = segment.segment(
            meta, cues, meta.duration, rep, options.subject, options.grade
        )
        segment.save(kps, segment_path, {"topic": topic, "subject_grade": subject_grade})
    if not kps:
        raise RuntimeError("没有切出任何知识点，可能是字幕为空或视频过短。")

    # 看图转写会把"观察到什么"写进标题（「画面上的原样标注为…」），
    # 那些是看图笔记不是标题，印在 Word 上很难看。这里先洗一遍，
    # 后面所有环节（包括标题臆造告警）看到的都是干净标题。
    for kp in kps:
        kp.title = textnorm.tidy_title(kp.title)
        for ex in kp.examples:
            ex.title = textnorm.tidy_title(ex.title)

    # 结构体检：重复例题、时间越界这类问题就地修掉。
    # 以前它们一路带到审核，回炉又改不动（回炉只能改文字，不能删题），
    # 于是每轮都报同一条，两轮下来一模一样——白烧二十分钟。
    struct_notes = normalize.normalize_kps(kps, meta.duration)
    struct_notes += normalize.suspicious_pairs(kps)
    for note in struct_notes[:12]:
        log(f"结构体检：{note}")
    if len(struct_notes) > 12:
        log(f"结构体检：……另有 {len(struct_notes) - 12} 条")

    segment.save(kps, segment_path, {"topic": topic, "subject_grade": subject_grade})

    # 标题里的括注标签如果字幕从没逐字出现过，先提醒老师看一眼。
    # 不自动删——ASR 噪声重的时候整串匹配会误伤真标签（实测过）。
    suspect = _find_ungrounded_labels(kps, cues)
    if suspect and log:
        log("标题核对：以下标签在字幕里没有逐字出现，请老师确认是不是臆造 → "
            + "、".join(suspect[:4]))

    # ---------------------------------------------------------- 4. 截例题画面
    rep = _Reporter("shots", on_progress, log)
    candidates = _load_candidates(work_dir) if options.reuse else {}
    missing = [ex.id for kp in kps for ex in kp.examples if ex.id not in candidates]
    if missing or not candidates:
        if candidates and missing:
            log(f"复用 {len(candidates) - len(missing)} 张已有截图，补截 {len(missing)} 张")
        candidates = shots.capture_examples(video, kps, shots_dir, meta.duration, rep)
        shots.capture_boards(video, kps, shots_dir, meta.duration, rep)
        _save_candidates(work_dir, candidates)

    # ---------------------------------------------------------- 5. 看图转写题目
    rep = _Reporter("vision", on_progress, log)
    todo = [ex for kp in kps for ex in kp.examples if not ex.statement_latex]
    if options.reuse and not todo:
        rep(1.0, "复用已转写的题干")
    else:
        vision.transcribe_examples(
            kps, candidates, cues, meta.title, shots_dir,
            progress=lambda f, m: rep(f, m),
        )
        segment.save(kps, segment_path, {"topic": topic, "subject_grade": subject_grade})

    # 看图之后先分清"真例题"和"老师只是口述"：切分阶段为了"题目全覆盖"
    # 会把每段概念讲解都立成例题，不在这里拦一道，学生的「即学即练」里
    # 就会全是只有标题、没有题干的空壳。
    for note in normalize.classify_exercises(kps)[:12]:
        log(f"例题甄别：{note}")
    talk = sum(1 for kp in kps for ex in kp.examples if not ex.is_exercise)
    real = sum(1 for kp in kps for ex in kp.examples if ex.is_exercise)
    if talk:
        log(f"例题甄别：{real} 道真例题 / {talk} 段口述讲解（口述部分只进教师版的讲解要点）")
    segment.save(kps, segment_path, {"topic": topic, "subject_grade": subject_grade})

    # ---------------------------------------------------------- 6. 生成教案
    rep = _Reporter("compose", on_progress, log)
    lesson_meta = LessonMeta(
        title=topic or meta.title,
        subject=options.subject or _first(subject_grade, "数学"),
        grade=options.grade or _second(subject_grade, ""),
        duration_min=options.duration_min,
        source_url=meta.webpage_url or meta.url,
        source_title=meta.title,
        uploader=meta.uploader,
        video_duration=meta.duration,
    )

    # 草稿单独存一份：compose 要跑十来分钟，审核又得跑二十几分钟，
    # 中间任何一次崩溃/休眠，之前算出来的东西全在内存里陪葬。
    # 之前连续两次白跑一小时，就是死在这。
    draft_path = work_dir / "lesson.draft.json"
    lesson = _load_draft(draft_path, lesson_meta, options.reuse, log)
    if lesson is None:
        # 每批例题完成就落一次盘：中途任何一批全灭都会让 compose 抛异常，
        # 没有中途存档的话，骨架那几分钟的算力就得白扔。
        def _save_partial(partial) -> None:
            try:
                compose.save(partial, draft_path)
            except OSError:
                pass

        lesson = compose.compose(
            lesson_meta, kps, cues, options.duration_min, rep, on_partial=_save_partial
        )
        compose.save(lesson, draft_path)
    else:
        rep(1.0, f"复用已有教案草稿：{len(lesson.knowledge)} 知识点 / "
                 f"{lesson.example_count()} 例题")

    # ---------------------------------------------------------- 7. 审核
    if options.do_review:
        rep = _Reporter("review", on_progress, log)

        def _checkpoint(done: Lesson) -> None:
            """每轮回炉完就存一次，别等全部跑完。"""
            try:
                compose.save(done, draft_path)
            except OSError as exc:
                log(f"⚠ 中间结果保存失败（不影响本次产出）：{exc}")

        lesson = review_stage.run_review_loop(
            lesson, cues, shots_dir, meta.duration, options.max_review_rounds,
            progress=lambda f, m: rep(f, m),
            on_lesson=_checkpoint,
        )
    else:
        log("已跳过审核环节")

    # ---------------------------------------------------------- 8. 导出
    rep = _Reporter("export", on_progress, log)
    teacher_docx, student_docx = export.export_docx_both(lesson, shots_dir, work_dir)
    # 成品已经挪到项目外面去了，任务目录里就找不到那两份 docx。
    # 留一份清单，历史页才知道去哪儿取——不然删掉 workspace 之外
    # 的文件之前，网页上的"下载"会突然 404。
    try:
        (work_dir / "export.json").write_text(json.dumps(
            {"docx": str(teacher_docx), "student_docx": str(student_docx)},
            ensure_ascii=False, indent=2,
        ), encoding="utf-8")
    except OSError:
        pass
    rep(0.6, f"教师备课版已生成：{teacher_docx.name}")
    rep(0.7, f"学生讲义已生成：{student_docx.name}")
    html_path = export.export_html(lesson, shots_dir, work_dir / f"{teacher_docx.stem}.html")
    rep(0.85, f"HTML 预览已生成：{html_path.name}")
    student_html = export.export_html(lesson, shots_dir,
                                      work_dir / f"{student_docx.stem}.html", audience="student")
    rep(1.0, "两份文档都已就绪")

    compose.save(lesson, work_dir / "lesson.json")

    elapsed = time.time() - t0
    log(f"全部完成，用时 {elapsed / 60:.1f} 分钟")
    return JobResult(
        job_id=work_dir.name,
        work_dir=work_dir,
        meta=meta,
        lesson=lesson,
        docx_path=teacher_docx,
        html_path=html_path,
        student_docx_path=student_docx,
        student_html_path=student_html,
        elapsed_sec=elapsed,
        logs=logs,
    )


# ------------------------------------------------------------------ 候选帧缓存
def _save_candidates(work_dir: Path, candidates: dict[str, list[Path]]) -> None:
    payload = {k: [p.name for p in v] for k, v in candidates.items() if v}
    (work_dir / "candidates.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _load_candidates(work_dir: Path) -> dict[str, list[Path]]:
    path = work_dir / "candidates.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    shots_dir = work_dir / "shots"
    out: dict[str, list[Path]] = {}
    for key, names in payload.items():
        resolved = [shots_dir / n for n in names if (shots_dir / n).exists()]
        if resolved:
            out[key] = resolved
    return out


# ------------------------------------------------------------------ 教案草稿缓存
def _load_draft(path: Path, want: LessonMeta, reuse: bool, log: Logger | None) -> Lesson | None:
    """读回上次算好的教案草稿（还没过审核的那版）。

    学科/年级/时长对不上就作废——这几个字段直接进教案的 meta 和顶层内容，
    换了还复用旧稿，等于交一份标题对不上的东西。
    """
    if not reuse or not path.exists():
        return None
    try:
        cached = compose.load(path)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"教案草稿读取失败，将重新生成（{type(exc).__name__}）")
        return None
    if not cached.knowledge:
        return None
    # 缓存里躺着一份空壳草稿的话，宁可重算——它比没有缓存更糟，
    # 会让重跑直接产出一份什么都没有的教案。
    try:
        bad = compose._degenerate(cached)
    except Exception:  # noqa: BLE001
        bad = ""
    if bad:
        if log:
            log(f"教案草稿是空壳（{bad}），忽略并重新生成")
        return None
    same = (
        (cached.meta.subject or "") == (want.subject or "")
        and (cached.meta.grade or "") == (want.grade or "")
        and abs(float(cached.meta.duration_min or 0) - float(want.duration_min or 0)) < 1e-6
    )
    if not same:
        if log:
            log("教案草稿的学科/年级/时长与本次不一致，重新生成")
        return None
    if log:
        log(f"复用教案草稿：{len(cached.knowledge)} 知识点 / "
            f"{cached.example_count()} 例题（审核环节会重跑）")
    return cached


def _first(text: str, default: str) -> str:
    """取 "学科 · 年级" 的前半；这一端是空的就用 default。

    靠 "·" 分辨两端，segment 那边即使只有一端有值也保留分隔符。
    取空之后必须走 default，否则空串会被原样写进教案首页。
    """
    return (text.split("·")[0].strip() or default) if text else default


def _second(text: str, default: str) -> str:
    parts = text.split("·") if text else []
    return (parts[1].strip() or default) if len(parts) > 1 else default