"""阶段 1：解析链接 + 下载视频（yt-dlp）。"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.config import SETTINGS
from app.media import ffmpeg

Progress = Callable[[float, str], None]

QUALITY_MAP = {
    "best": None,
    "2160": "2160",
    "1440": "1440",
    "1080": "1080",
    "720": "720",
    "480": "480",
    "360": "360",
}

BILI_PATTERNS = [
    re.compile(r"bilibili\.com/video/(BV[0-9A-Za-z]{10})"),
    re.compile(r"bilibili\.com/video/(av\d+)"),
    re.compile(r"b23\.tv/([0-9A-Za-z]+)"),
]


class FetchError(RuntimeError):
    pass


@dataclass
class VideoMeta:
    url: str
    source_id: str = ""
    title: str = ""
    uploader: str = ""
    duration: float = 0.0
    description: str = ""
    webpage_url: str = ""
    is_bilibili: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "source_id": self.source_id,
            "title": self.title,
            "uploader": self.uploader,
            "duration": self.duration,
            "description": self.description,
            "webpage_url": self.webpage_url,
            "is_bilibili": self.is_bilibili,
        }


def parse_source_id(url: str) -> tuple[str, bool]:
    """从链接里抠出视频 id，顺便判断是不是 B 站。"""
    for pat in BILI_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1), True
    return "", False


def _format_opts() -> dict:
    """画质限制交给 yt-dlp 的 format_sort，它自带降级链，比手写格式串稳。

    注意：只对视频流加 height 限制。音频流没有 height 元数据，加了反而匹配不到，
    会静默地只下到一个"纯视频"文件，转写时才发现没声音。
    """
    limit = QUALITY_MAP.get(SETTINGS.video_quality)
    if limit is None:
        return {"format": "bv*+ba/b"}
    return {
        "format": f"bv*[height<={limit}]+ba/"
                  f"bv*[height<={limit}]+b[ext=m4a]/"
                  f"b[height<={limit}]+ba/"
                  f"bv*+ba/b",
        "format_sort": [f"res:{limit}", "ext:mp4:m4a", "size"],
    }


def has_audio_stream(path: Path) -> bool:
    """下载后必须校验：文件里得有音轨，否则转写这一步会白跑。"""
    import subprocess

    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
        return bool((proc.stdout or "").strip())
    except Exception:  # noqa: BLE001
        return False


def _is_local_source(url: str) -> bool:
    if url.startswith("file://"):
        return True
    try:
        return Path(url).is_file()
    except OSError:
        return False


def _local_video(url: str, work_dir: Path, progress: Progress | None = None) -> tuple[VideoMeta, Path]:
    """直接用本地视频文件（网盘下载的课、自己录的屏都算）。"""
    raw = url[7:] if url.startswith("file://") else url
    src = Path(raw)
    if not src.is_file():
        raise FetchError(f"本地文件不存在：{src}")

    if progress:
        progress(0.1, f"使用本地视频：{src.name}")
    target = work_dir / f"source{src.suffix or '.mp4'}"
    # 已经复制过且大小一致就跳过，重跑时不用再拷一遍
    if src.resolve() != target.resolve():
        same = target.exists() and target.stat().st_size == src.stat().st_size
        if not same:
            shutil.copyfile(src, target)
        if progress:
            state = "已复用" if same else "已复制"
            progress(1.0, f"{state}到工作目录（{target.stat().st_size / 1_048_576:.0f} MB）")

    duration = ffmpeg.probe_duration(target)
    meta = VideoMeta(
        url=url,
        source_id=src.stem,
        title=src.stem,
        uploader="本地文件",
        duration=duration,
        webpage_url=url,
        is_bilibili=False,
    )
    meta_cache = work_dir / "meta.json"
    if not meta_cache.exists():
        _write_meta(meta_cache, meta)
    return meta, target


def _is_cookie_problem(exc: Exception) -> bool:
    """判断这个异常是不是"读浏览器 Cookie 失败"，而不是真的下不动。"""
    low = str(exc).lower()
    return any(k in low for k in (
        "dpapi", "cookies database", "could not find", "cookie",
        "failed to decrypt", "could not copy",
    ))


def _meta_from(info: dict, url: str) -> VideoMeta:
    meta = VideoMeta(
        url=url,
        source_id=str(info.get("id") or parse_source_id(info.get("webpage_url", url))[0]),
        title=info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=float(info.get("duration") or 0.0),
        description=(info.get("description") or "")[:4000],
        webpage_url=info.get("webpage_url") or url,
        is_bilibili="bilibili" in (info.get("webpage_url") or url),
    )
    meta.extra = {
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
    }
    return meta


def _finish_fetch(work_dir: Path, opts: dict, url: str, meta: VideoMeta,
                  progress: Progress | None) -> Path:
    """下载完的收尾：定位文件、校验音轨、必要时换格式重下。"""
    video_path = _find_existing_video(work_dir)
    if video_path is None:
        raise FetchError("下载结束但没找到视频文件，可能是清晰度受限或需要登录。")

    if not has_audio_stream(video_path):
        if progress:
            progress(0.5, "⚠ 下载到的文件没有音轨，改用不限制画质的格式重试…")
        _clear_media(work_dir)
        retry_opts = {**opts, "format": "bv*+ba/b"}
        retry_opts.pop("format_sort", None)
        # 这里必须自己导入：fetch_video 里那次是函数内局部导入，
        # 出了那个函数就看不见了。以前这条重试路径一走就 NameError 崩掉，
        # 而"下到的视频没音轨"恰恰是未登录时最常见的坑，等于直接没救。
        from yt_dlp import YoutubeDL

        with YoutubeDL(retry_opts) as ydl:
            ydl.download([url])
        video_path = _find_existing_video(work_dir)
        if video_path is None or not has_audio_stream(video_path):
            raise FetchError(
                "拿到的视频没有音轨。B 站未登录时部分清晰度不提供音频，"
                "可以在 .env 里设置 BILI_COOKIES_FROM_BROWSER 借用浏览器登录态。"
            )
    return video_path


def _friendly_download_error(exc: Exception) -> str:
    """把 yt-dlp 的原始报错翻译成能照着做的指引。

    B 站的 412 对用户来说毫无信息量，但成因其实很集中：没登录 / IP 被风控 /
    Cookie 库被浏览器锁着。这里按症状分流。
    """
    text = str(exc)
    low = text.lower()

    if "412" in text:
        return (
            "B 站拒绝了这次请求（HTTP 412）。这不是视频不存在，是没通过它的风控。\n"
            "按省事程度依次试：\n"
            "  ① 用浏览器登录 B 站后，完全关掉浏览器，再重试；"
            "并在 .env 设 BILI_COOKIES_FROM_BROWSER=edge（edge:Default 或 edge:Profile 1）。\n"
            "  ② 挂个代理或换网络。\n"
            "  ③ 用别的工具把视频下到本地，再把本地文件路径粘到链接框里——"
            "本地视频这条路完全不经过 B 站，现在就能用。"
        )

    if "cookies database" in low or "dpapi" in low or "could not find" in low:
        return (
            "读不到浏览器 Cookie。B 站的登录态是从浏览器借的，两个常见坑：\n"
            "  ① 浏览器还开着会锁住 Cookie 库——请完全退出浏览器再重试；\n"
            "  ② .env 里的浏览器名要写对，这台机器装的是 Edge，"
            "所以写 edge 或 edge:Default / edge:Profile 1，不是 chrome。\n"
            "  也可以改用 BILI_COOKIES_FILE 指向一个 cookies.txt。"
        )

    if "private" in low or "大会员" in text or "会员" in text:
        return "这个视频需要大会员或付费，请换一个公开课视频。"

    return f"下载失败：{text[:400]}"


def fetch_video(url: str, work_dir: Path, progress: Progress | None = None) -> tuple[VideoMeta, Path]:
    """下载视频到 work_dir/source.mp4，返回元数据与文件路径。

    已经下载过就直接复用（同一链接重复生成教案时能省一大截时间）。
    """
    from yt_dlp import YoutubeDL

    work_dir.mkdir(parents=True, exist_ok=True)
    existing = _find_existing_video(work_dir)
    meta_cache = work_dir / "meta.json"

    if _is_local_source(url):
        return _local_video(url, work_dir, progress)

    # 视频和元数据都已经在本地就直接返回，**一个网络请求都不发**。
    #
    # 以前这里虽然算出了 existing，却还是照样去 B 站 extract_info 拉元数据，
    # 结果：素材明明齐了，一样会被风控拦。实测 HTTP 412，整条任务在第 1 步就挂，
    # 而本地 source.mp4（190MB）和 meta.json 都在，白白重跑。
    cached = _load_meta_cache(meta_cache)
    if existing is not None and cached is not None and cached.duration > 0:
        if progress:
            progress(1.0, f"复用已下载的视频（{existing.stat().st_size / 1048576:.0f}MB）")
        return cached, existing

    state: dict = {}

    def hook(status: dict) -> None:
        if not progress:
            return
        if status.get("status") == "downloading":
            total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
            done = status.get("downloaded_bytes") or 0
            pct = (done / total) if total else 0.0
            speed = status.get("speed") or 0
            mbps = speed / 1_048_576 if speed else 0
            progress(min(0.99, pct), f"下载视频 {done / 1_048_576:.0f}MB{'/' + str(int(total / 1_048_576)) + 'MB' if total else ''}"
                     f"（{mbps:.1f} MB/s）")
        elif status.get("status") == "finished":
            progress(1.0, "视频下载完成")

    opts = {
        "merge_output_format": "mp4",
        "outtmpl": str(work_dir / "source.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "ffmpeg_location": str(Path(SETTINGS.ffmpeg).parent) if SETTINGS.ffmpeg else None,
        "retries": 5,
        "concurrent_fragment_downloads": 4,
        "http_headers": {
            "Referer": "https://www.bilibili.com/",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
        },
        **_format_opts(),
    }
    if SETTINGS.cookies_from_browser:
        opts["cookiesfrombrowser"] = (SETTINGS.cookies_from_browser,)
    cookie_file = os.getenv("BILI_COOKIES_FILE", "").strip()
    if cookie_file and Path(cookie_file).is_file():
        # 手工导出的 Netscape 格式 cookies.txt，比借浏览器省事，也不会被 Edge 锁库
        opts["cookiefile"] = cookie_file

    try:
        with YoutubeDL(opts) as ydl:
            if progress:
                progress(0.02, "解析视频信息…")
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                entries = [e for e in (info.get("entries") or []) if e]
                if not entries:
                    raise FetchError("这个链接是一个空合集，里面没有视频。")
                info = entries[0]
            meta = VideoMeta(
                url=url,
                source_id=str(info.get("id") or parse_source_id(info.get("webpage_url", url))[0]),
                title=info.get("title") or "",
                uploader=info.get("uploader") or info.get("channel") or "",
                duration=float(info.get("duration") or 0.0),
                description=(info.get("description") or "")[:4000],
                webpage_url=info.get("webpage_url") or url,
                is_bilibili="bilibili" in (info.get("webpage_url") or url),
            )
            meta.extra = {
                "width": info.get("width"),
                "height": info.get("height"),
                "fps": info.get("fps"),
            }
            if progress:
                progress(0.05, f"《{meta.title}》时长 {_hms(meta.duration)}，开始下载")
            ydl.download([url])
    except FetchError:
        raise
    except Exception as exc:  # noqa: BLE001
        # Cookie 只是优化项，不是必需品。浏览器开着读不出来时，
        # 直接退回匿名请求，而不是整条任务失败——很多公开课匿名就能下。
        if "cookiesfrombrowser" in opts and _is_cookie_problem(exc):
            if progress:
                progress(0.03, "读不到浏览器登录态，改用匿名请求重试…")
            opts.pop("cookiesfrombrowser", None)
            try:
                with YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if info and info.get("_type") == "playlist":
                        entries = [e for e in (info.get("entries") or []) if e]
                        if not entries:
                            raise FetchError("这个链接是一个空合集，里面没有视频。")
                        info = entries[0]
                    meta = _meta_from(info, url)
                    ydl.download([url])
            except Exception as exc2:  # noqa: BLE001
                raise FetchError(_friendly_download_error(exc2)) from exc2
            state["meta"] = meta
            video_path = _finish_fetch(work_dir, opts, url, meta, progress)
            _write_meta(meta_cache, meta)
            return meta, video_path
        raise FetchError(_friendly_download_error(exc)) from exc

    state["meta"] = meta
    video_path = _finish_fetch(work_dir, opts, url, meta, progress)
    _write_meta(meta_cache, meta)
    return meta, video_path


def _write_meta(meta_cache: Path, meta: VideoMeta) -> None:
    import json

    meta_cache.write_text(json.dumps(meta.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")


def _load_meta_cache(meta_cache: Path) -> VideoMeta | None:
    """读回上次写下的元数据。坏了就返回 None，让它照常走网络。"""
    import json

    if not meta_cache.exists():
        return None
    try:
        return VideoMeta(**json.loads(meta_cache.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return None


def _clear_media(work_dir: Path) -> None:
    for hit in work_dir.glob("source.*"):
        if hit.is_file():
            try:
                hit.unlink()
            except OSError:
                pass


def _find_existing_video(work_dir: Path) -> Path | None:
    for pat in ("source.mp4", "source.webm", "source.mkv", "source.flv", "source.*"):
        for hit in sorted(work_dir.glob(pat)):
            if hit.is_file() and hit.stat().st_size > 1024:
                return hit
    # 合并失败时可能留下分离的视频流
    for hit in sorted(work_dir.glob("source.f*.mp4")) + sorted(work_dir.glob("source.f*.webm")):
        if hit.is_file() and hit.stat().st_size > 1024:
            return hit
    return None


def _hms(seconds: float) -> str:
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def slugify(text: str, limit: int = 24) -> str:
    clean = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", text).strip("-")
    return (clean[:limit] or "lesson").strip("-")