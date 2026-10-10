"""候选帧打分：从 1fps 的连续帧里挑出"最可能是完整板书/完整题目"的几帧。

思路：整屏内容突变 = 老师切页/翻到新题；画面稳定 = 题目刚写完还没擦。
真正"题干完整"这件事交给多模态模型判断，这里只负责把最有可能的帧排到前面。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass
class FrameInfo:
    path: Path
    index: int
    ts: float
    diff_prev: float = 0.0
    score: float = 0.0
    reason: str = ""


def _fingerprint(path: Path, size: tuple[int, int] = (64, 36)) -> np.ndarray:
    with Image.open(path) as img:
        return np.asarray(img.convert("L").resize(size, Image.BILINEAR), dtype=np.float32) / 255.0


def analyze_frames(
    frames: list[Path],
    window_start: float,
    fps: float = 1.0,
    scene_threshold: float = 0.085,
) -> list[FrameInfo]:
    """算出每帧与其前一帧的差异量，并标记画面突变的位置。"""
    infos: list[FrameInfo] = []
    prev_fp: np.ndarray | None = None
    for i, path in enumerate(frames):
        fp = _fingerprint(path)
        diff = 0.0 if prev_fp is None else float(np.mean(np.abs(fp - prev_fp)))
        infos.append(
            FrameInfo(
                path=path,
                index=i,
                ts=window_start + i / fps,
                diff_prev=diff,
            )
        )
        prev_fp = fp
    return infos


def is_scene_change(info: FrameInfo, threshold: float) -> bool:
    return info.diff_prev >= threshold


def _ink_load(infos: list[FrameInfo], window: int = 8) -> list[float]:
    """每一帧的"板书密度"：最近若干帧的累计变化量。

    画面刚切到题面时累计变化接近 0；老师一边讲一边在旁边推导，这个值会一路涨。
    题目越干净，这个值越小——用来把"刚出题"和"已经讲了一半"区分开。
    """
    out: list[float] = []
    for i in range(len(infos)):
        start = max(0, i - window + 1)
        out.append(sum(x.diff_prev for x in infos[start : i + 1]))
    return out


def score_candidates(
    infos: list[FrameInfo],
    target_ts: float,
    scene_threshold: float = 0.085,
    top_k: int = 4,
    min_gap: float = 4.0,
) -> list[FrameInfo]:
    """按"离目标时刻的距离 + 是否刚发生画面切换 + 板书有多干净"综合打分。"""
    if not infos:
        return []

    changes = [i.index for i in infos if is_scene_change(i, scene_threshold)]
    change_set = set(changes)
    ink = _ink_load(infos)
    ink_max = max(ink) or 1.0

    scored: list[FrameInfo] = []
    for idx, info in enumerate(infos):
        # 1) 时间贴合：题目通常在"老师宣布开始讲题"之后 2~30 秒出现
        delay = info.ts - target_ts
        if delay < -2:
            time_score = 0.05
        else:
            time_score = math.exp(-((delay - 6.0) ** 2) / (2 * (14.0 ** 2)))

        # 2) 刚切换过画面 → 新内容刚完整铺出来
        just_changed = 0.0
        for back in range(0, 4):
            if (idx - back) in change_set:
                just_changed = max(just_changed, 1.0 - back * 0.25)
                break

        # 3) 板书干净程度：累计变化越小越好（刚出题 vs 已经讲了一半）
        cleanliness = 1.0 - (ink[idx] / ink_max)
        # 4) 稳定度：这一帧相对前一帧变化越小越"写完了"
        stability = 1.0 / (1.0 + info.diff_prev * 12.0)

        # 5) 轻微加权：太靠后的帧（老师已经开始讲下一步）扣一点
        tail_penalty = 0.25 if delay > 38 else 0.0

        info.score = (
            0.34 * time_score
            + 0.26 * just_changed
            + 0.22 * cleanliness
            + 0.18 * stability
            - tail_penalty
        )
        bits = [f"时间贴合={time_score:.2f}"]
        if just_changed:
            bits.append("画面刚切换")
        bits.append(f"板书干净={cleanliness:.2f}")
        bits.append(f"稳定度={stability:.2f}")
        if tail_penalty:
            bits.append("偏后")
        info.reason = "，".join(bits)
        scored.append(info)

    scored.sort(key=lambda x: x.score, reverse=True)

    picked: list[FrameInfo] = []
    for info in scored:
        if len(picked) >= top_k:
            break
        if all(abs(info.ts - p.ts) >= min_gap for p in picked):
            picked.append(info)

    # 保证至少有一帧贴近目标时刻
    if not picked:
        picked = [min(scored, key=lambda x: abs(x.ts - target_ts))]
    return picked


def pick_board_frame(
    infos: list[FrameInfo],
    scene_threshold: float = 0.085,
    tail_fraction: float = 0.55,
) -> FrameInfo | None:
    """挑"知识点讲解结束、板书最完整"的那一帧：窗口后段里，画面稳定且接近末尾的一帧。"""
    if not infos:
        return None
    lo = int(len(infos) * tail_fraction)
    tail = infos[lo:]
    if not tail:
        tail = infos[-1:]

    # 末尾一段内，画面最稳定且时间靠后 → 板书写完了
    last_change = max(
        (i.index for i in tail if is_scene_change(i, scene_threshold)),
        default=tail[0].index - 1,
    )
    settled = [i for i in tail if i.index > last_change] or tail

    best = max(settled, key=lambda i: (i.index / max(1, len(infos)), -i.diff_prev))
    best.reason = "窗口后段画面稳定，板书最完整"
    return best