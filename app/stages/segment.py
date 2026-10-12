"""阶段 3：把字幕切成结构化知识点，并定位每道例题的时间点。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from app.llm.client import MiniMaxClient
from app.llm.prompts import SEGMENT_SYSTEM, SEGMENT_USER
from app.models import Cue, Example, KnowledgePoint

Progress = Callable[[float, str], None]


def _as_float(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().rstrip("s")
            if ":" in value:  # 00:01:23
                parts = [float(p) for p in value.split(":")]
                total = 0.0
                for p in parts:
                    total = total * 60 + p
                return total
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [line.strip(" -•·\t") for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or item.get("point")
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
        return out
    return []


def segment(
    meta,
    cues: list[Cue],
    duration: float,
    progress: Progress | None = None,
    subject_hint: str = "",
    grade_hint: str = "",
) -> tuple[str, str, list[KnowledgePoint]]:
    """返回 (课题, 学科年级, 知识点列表)。"""
    if not cues:
        raise ValueError("没有字幕，无法切分知识点。")

    client = MiniMaxClient()
    transcript = "\n".join(f"[{_hms(c.start)}] {c.text}" for c in cues)

    extra = ""
    if meta.title:
        extra += f"视频标题里可能含有学科/年级线索：《{meta.title}》\n"
    if subject_hint:
        extra += f"用户指定的学科：{subject_hint}\n"
    if grade_hint:
        extra += f"用户指定的年级：{grade_hint}\n"

    user_msg = SEGMENT_USER.format(
        title=meta.title or "未知",
        uploader=meta.uploader or "未知",
        duration=_hms(duration),
        duration_sec=f"{duration:.1f}",
        transcript=transcript,
    )
    if extra:
        user_msg = extra + "\n" + user_msg

    if progress:
        progress(0.15, f"字幕共 {len(cues)} 段 / {len(transcript)} 字，交给模型切分知识点…")

    data = client.chat_json(
        [{"role": "system", "content": SEGMENT_SYSTEM}, {"role": "user", "content": user_msg}],
        temperature=0.1,
        # 这一步是"在字幕里找板块边界"的机械活，不需要深度思考。
        # 开着 adaptive thinking 时，25000 字字幕会把 600s 超时撑爆
        # （实测整节课的转写卡在这里，重试 5 次全灭）。
        thinking="disabled",
        label="segment",
    )

    raw_segments: list[dict] = []
    if isinstance(data, list):
        raw_segments = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        raw_segments = data.get("segments") or data.get("knowledge_points") or data.get("knowledge") or []
        if not raw_segments and ("title" in data or "ts_start" in data):
            raw_segments = [data]

    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError(f"模型没有切出任何知识点：{str(data)[:300]}")

    kps: list[KnowledgePoint] = []
    ex_counter = 0
    for i, seg in enumerate(raw_segments, 1):
        if not isinstance(seg, dict):
            continue
        kp = KnowledgePoint(
            id=str(seg.get("id") or f"kp{i}"),
            title=str(seg.get("title") or f"知识点 {i}").strip(),
            ts_start=_as_float(seg.get("ts_start"), 0.0),
            ts_end=_as_float(seg.get("ts_end"), 0.0),
            summary=str(seg.get("summary") or "").strip(),
            definition=str(seg.get("definition") or "").strip(),
            formula_latex=str(seg.get("formula_latex") or "").strip(),
            key_points=_as_list(seg.get("key_points")),
            pitfalls=_as_list(seg.get("pitfalls")),
            source_quote=str(seg.get("source_quote") or "").strip(),
        )
        for raw_ex in seg.get("examples") or []:
            if not isinstance(raw_ex, dict):
                continue
            ex_counter += 1
            ex = Example(
                id=str(raw_ex.get("id") or f"ex{ex_counter}"),
                title=str(raw_ex.get("title") or f"例{ex_counter}").strip(),
                ts_start=_as_float(raw_ex.get("ts_start"), kp.ts_start),
                ts_end=_as_float(raw_ex.get("ts_end"), 0.0),
                notes=str(raw_ex.get("hint") or "").strip(),
            )
            if ex.ts_end <= ex.ts_start:
                ex.ts_end = min(duration, ex.ts_start + 120.0)
            kp.examples.append(ex)
        kps.append(kp)

    kps = _normalize(kps, duration)

    topic = str(data.get("topic") or "").strip() if isinstance(data, dict) else ""
    subject = subject_hint or (str(data.get("subject") or "").strip() if isinstance(data, dict) else "")
    grade = grade_hint or (str(data.get("grade") or "").strip() if isinstance(data, dict) else "")
    if not topic and kps:
        topic = kps[0].title

    if progress:
        total_ex = sum(len(kp.examples) for kp in kps)
        progress(1.0, f"切出 {len(kps)} 个知识点、{total_ex} 道例题")
    # 中间的 "·" 必须留着：只有一端有值时也要写成 " · 高一" / "数学 · "，
    # 下游才能靠分隔符分清哪端是空的。以前这里 .strip(" ·") 把分隔符也
    # 去了，" · 高一" 塌成 "高一"，学科取出来就变成年级。
    return topic, f"{subject} · {grade}", kps


def _normalize(kps: list[KnowledgePoint], duration: float) -> list[KnowledgePoint]:
    """修时间轴：夹紧到视频时长、排好序、切开重叠、补上空洞、拖到片尾。

    这一段以前有两个真 bug，都是"看起来在修、其实越修越坏"：

    1) 重叠修补的循环写的是 `if kp.ts_end > ...`，而 `kp` 是上一个
       `for i, kp in enumerate(kps)` 循环泄漏出来的变量，永远等于**最后一个**
       知识点。结果中间的知识点从来没被切开过（a 0-100 和 b 50-200 原样保留），
       反倒是最后一个被反复砍到 1 秒（c 结束于 300 → 变成 181）。
    2) 只夹紧、不补覆盖。提示词里"最后一个板块到达视频结尾"这种话靠不住，
       模型经常只切出视频的前一半。实测 360 秒的视频只切出 0-178.5 秒，
       老师 317-358 秒的抢答题整段落在覆盖之外——没有知识点、没有例题、
       没有截图，那道题在教案里彻底消失，日志上还看不出少了什么。
    """
    kps = [kp for kp in kps if kp.title or kp.summary]
    if not kps:
        return kps
    kps.sort(key=lambda k: k.ts_start)

    # 1) 夹紧到 [0, duration]，零长/倒挂的板块先撑开
    for i, kp in enumerate(kps):
        kp.ts_start = max(0.0, min(kp.ts_start, duration))
        kp.ts_end = max(0.0, min(kp.ts_end or duration, duration))
        if kp.ts_end <= kp.ts_start:
            nxt = kps[i + 1].ts_start if i + 1 < len(kps) else duration
            kp.ts_end = min(duration, max(kp.ts_start, nxt))
            if kp.ts_end <= kp.ts_start:
                kp.ts_end = min(duration, kp.ts_start + 1.0)

    # 2) 切开重叠：后一块的开始就是前一块的结束
    #    这里必须用 kps[i]。写成 kp 会拿到上一个循环泄漏出来的最后一个元素。
    for i in range(len(kps) - 1):
        a, b = kps[i], kps[i + 1]
        if a.ts_end > b.ts_start:
            a.ts_end = b.ts_start
        if a.ts_end <= a.ts_start:
            a.ts_end = min(duration, max(a.ts_start + 1.0, b.ts_start))

    # 3) 补空洞：中间没被任何板块覆盖的时间归前一块
    for i in range(len(kps) - 1):
        a, b = kps[i], kps[i + 1]
        if a.ts_end < b.ts_start:
            a.ts_end = b.ts_start

    # 4) 拖到片尾：最后一个板块一直盖到视频结束
    if duration > 0 and kps[-1].ts_end < duration:
        kps[-1].ts_end = duration
    return kps


def coverage_note(kps: list[KnowledgePoint], duration: float) -> str | None:
    """切分没盖住整段视频时给一句人话提醒；盖满了返回 None。"""
    if duration <= 0 or not kps:
        return None
    lo = min(kp.ts_start for kp in kps)
    hi = max(kp.ts_end for kp in kps)
    covered = sum(max(0.0, kp.ts_end - kp.ts_start) for kp in kps)
    ratio = covered / (hi - lo) if hi > lo else 0.0
    if lo <= 1.0 and hi >= duration - 1.0 and ratio >= 0.97:
        return None
    return (
        f"切分只覆盖了 {ratio:.0%} 的视频（{lo:.0f}-{hi:.0f}s / 全片 {duration:.0f}s）。"
        f"没覆盖到的部分不会被抽帧、也不会出例题，请确认老师是不是在片尾出了题。"
    )


def save(kps: list[KnowledgePoint], path: Path, extra: dict | None = None) -> None:
    payload = {"knowledge": [kp.model_dump() for kp in kps], **(extra or {})}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"