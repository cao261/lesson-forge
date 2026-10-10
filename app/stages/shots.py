"""阶段 4：为每道例题截"题目完整出现"的那一帧，为每个知识点截"板书总结"的那一帧。

先在 [题目前几秒, 讲完前后] 的窗口里按 1fps 抽帧，
用画面突变（切页/翻到新题）+ 稳定性给每帧打分，取 top-k 交给多模态模型确认。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from app.config import SETTINGS
from app.media import ffmpeg
from app.media.frames import analyze_frames, pick_board_frame, score_candidates
from app.models import KnowledgePoint

Progress = Callable[[float, str], None]

MAX_NOTES = 400


def _merge_notes(existing: str, addition: str) -> str:
    """把诊断信息写进例题备注。重跑时不会无限追加，重复的会被替换。"""
    kept = [seg for seg in (existing or "").split(" | ") if seg and not seg.startswith(("画面候选：", "候选"))]
    kept.append(addition)
    return " | ".join(kept)[-MAX_NOTES:]


def capture_examples(
    video: Path,
    kps: list[KnowledgePoint],
    shots_dir: Path,
    duration: float,
    progress: Progress | None = None,
) -> dict[str, list[Path]]:
    """返回 {example_id: 候选帧路径列表}。"""
    shots_dir.mkdir(parents=True, exist_ok=True)
    total = sum(len(kp.examples) for kp in kps)
    done = 0
    result: dict[str, list[Path]] = {}

    for kp in kps:
        for ex in kp.examples:
            done += 1
            if progress:
                progress(
                    done / max(1, total) * 0.8,
                    f"截取 {kp.id}/{ex.id}「{ex.title}」画面（{_hms(ex.ts_start)}）",
                )
            lo = max(0.0, ex.ts_start - SETTINGS.shot_pre_roll)
            hi = min(duration, max(ex.ts_start + 8.0, ex.ts_end + SETTINGS.shot_post_roll))
            hi = min(hi, duration)
            if hi - lo < 3:
                hi = min(duration, lo + 3)
            try:
                frames = ffmpeg.extract_frames(video, lo, hi, shots_dir / f"_tmp_{ex.id}", fps=1.0)
            except Exception as exc:  # noqa: BLE001
                if progress:
                    progress(done / max(1, total) * 0.8, f"⚠ {ex.id} 抽帧失败：{exc}")
                result[ex.id] = []
                continue

            if not frames:
                result[ex.id] = []
                continue

            infos = analyze_frames(frames, window_start=lo, fps=1.0)
            picked = score_candidates(infos, target_ts=ex.ts_start, top_k=SETTINGS.shot_candidates)

            candidates: list[Path] = []
            diagnostics: list[str] = []
            for i, info in enumerate(picked, 1):
                dest = shots_dir / f"{ex.id}_c{i}.jpg"
                shutil.copyfile(info.path, dest)
                diagnostics.append(f"候选{i}@{_hms(info.ts)} {info.reason}")
                candidates.append(dest)
            shutil.rmtree(shots_dir / f"_tmp_{ex.id}", ignore_errors=True)
            ex.notes = _merge_notes(ex.notes, "画面候选：" + "；".join(diagnostics))
            result[ex.id] = candidates
            if progress and candidates:
                progress(done / max(1, total) * 0.8, f"{ex.id} 找到 {len(candidates)} 张候选帧")

    return result


def capture_boards(
    video: Path,
    kps: list[KnowledgePoint],
    shots_dir: Path,
    duration: float,
    progress: Progress | None = None,
) -> None:
    """给每个知识点截一张"板书最完整"的图，直接写进 kp.board_shot 对应的 example。"""
    shots_dir.mkdir(parents=True, exist_ok=True)
    for i, kp in enumerate(kps):
        if progress:
            progress(0.8 + 0.15 * (i + 1) / max(1, len(kps)), f"截取 {kp.id} 板书总结画面")
        first_ex = min((ex.ts_start for ex in kp.examples), default=kp.ts_end)
        lo = max(0.0, kp.ts_start)
        hi = min(duration, max(lo + 5.0, first_ex if first_ex > lo else kp.ts_end))
        if hi - lo < 3:
            hi = min(duration, lo + 3)
        try:
            frames = ffmpeg.extract_frames(video, lo, hi, shots_dir / f"_tmpb_{kp.id}", fps=0.5)
        except Exception:  # noqa: BLE001
            continue
        if not frames:
            continue
        infos = analyze_frames(frames, window_start=lo, fps=0.5)
        best = pick_board_frame(infos)
        if best:
            dest = shots_dir / f"{kp.id}_board.jpg"
            shutil.copyfile(best.path, dest)
            kp.board_shot = dest.name
        shutil.rmtree(shots_dir / f"_tmpb_{kp.id}", ignore_errors=True)


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"