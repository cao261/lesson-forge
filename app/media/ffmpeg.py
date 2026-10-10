"""ffmpeg 薄封装：抽音轨、抽帧、探测时长。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from app.config import SETTINGS

Progress = Callable[[float, str], None]


class FFmpegError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-1200:]
        raise FFmpegError(f"命令失败：{' '.join(cmd[:6])} ...\n{tail}")
    return proc


def ffmpeg_bin() -> str:
    return SETTINGS.ffmpeg or "ffmpeg"


def probe_duration(path: Path) -> float:
    proc = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", str(path),
        ],
        timeout=120,
    )
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except Exception as exc:  # noqa: BLE001
        raise FFmpegError(f"无法读取媒体时长：{path}（{exc}）") from exc


def extract_audio(video: Path, out_wav: Path, progress: Progress | None = None) -> Path:
    """抽 16k 单声道 wav —— Whisper 的最佳输入格式。"""
    if progress:
        progress(0.0, f"抽取音轨 → {out_wav.name}")
    run(
        [
            ffmpeg_bin(), "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(out_wav),
        ]
    )
    if progress:
        progress(1.0, "音轨抽取完成")
    return out_wav


def extract_frames(
    video: Path,
    start: float,
    end: float,
    out_dir: Path,
    fps: float = 1.0,
    quality: int = 3,
) -> list[Path]:
    """在 [start, end] 区间按 fps 抽帧到 out_dir，返回按时间排序的文件列表。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("f_*.jpg"):
        stale.unlink()
    span = max(1.0, end - start)
    run(
        [
            ffmpeg_bin(), "-y",
            "-ss", f"{max(0.0, start):.3f}", "-to", f"{end:.3f}",
            "-i", str(video),
            "-vf", f"fps={fps},scale='min(1280,iw)':-2",
            "-q:v", str(quality),
            str(out_dir / "f_%05d.jpg"),
        ]
    )
    return sorted(out_dir.glob("f_*.jpg"))


def extract_frame(video: Path, ts: float, out_png: Path, width: int = 1280) -> Path:
    """精确抽取某一时刻的单帧。"""
    out_png.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            ffmpeg_bin(), "-y",
            "-ss", f"{max(0.0, ts):.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale='min({width},iw)':-2",
            "-q:v", "2",
            str(out_png),
        ],
        timeout=300,
    )
    return out_png