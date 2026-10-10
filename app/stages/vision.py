"""阶段 5：多模态看图，把板书上的例题转写成 LaTeX。

对每道例题，按候选帧的优先级逐张喂给 MiniMax-M3，
直到有一张被确认为"这道题的完整题干"为止。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from app.llm.client import LLMError, MiniMaxClient
from app.llm.prompts import TRANSCRIBE_SYSTEM, TRANSCRIBE_USER, build_transcript_context
from app.models import Cue, Example, KnowledgePoint, fmt_ts

Progress = Callable[[float, str], None]

ACCEPT_CONFIDENCE = 0.45

# M3 开的是 adaptive 思考模式，转写一道题本身就要想很久，
# 6000 会被思考过程吃光、JSON 吐不出来（表现为"画面里没有题目"，极具迷惑性）。
VISION_MAX_TOKENS = 16000

# 画面上通常有大片黑边/白边和留白，裁掉能让正文字符大得多，识别更准
TRIM_THRESHOLD = 12          # 行列最大-最小差值小于这个值就认为是纯色边
TRIM_MARGIN = 6              # 裁完留几像素边，别贴着字
TARGET_WIDTH = 1400          # 放大到这个宽度即可；再大只会让模型想更久、token 更多


def prepare_frame(path: Path, target_width: int = TARGET_WIDTH) -> Path:
    """裁掉纯色边框并适度放大，让板书上的字尽量大。

    能降低模型把 "-2<x<m" 读成 "-2<x<-m" 这类少负号/多负号的概率。

    注意别放太大：M3 是深度思考模式，图越大它想得越久，
    很容易在吐出 JSON 之前就把 max_tokens 用光。
    """
    try:
        from PIL import Image, ImageChops
    except ImportError:
        return path

    try:
        with Image.open(path) as img:
            img = img.convert("RGB")
            gray = img.convert("L")
            bg = Image.new("RGB", img.size, gray.getpixel((0, 0)))
            diff = ImageChops.difference(img, bg).convert("L")
            bbox = diff.point(lambda p: 255 if p > TRIM_THRESHOLD else 0).getbbox()
            if bbox:
                left, top, right, bottom = bbox
                left = max(0, left - TRIM_MARGIN)
                top = max(0, top - TRIM_MARGIN)
                right = min(img.width, right + TRIM_MARGIN)
                bottom = min(img.height, bottom + TRIM_MARGIN)
                if right - left >= 32 and bottom - top >= 32:
                    img = img.crop((left, top, right, bottom))

            if img.width < target_width:
                scale = min(2.5, target_width / img.width)
                img = img.resize(
                    (int(img.width * scale), int(img.height * scale)), Image.LANCZOS
                )
            out = path.with_name(path.stem + "_prep.png")
            img.save(out, format="PNG")
            return out
    except Exception:  # noqa: BLE001
        return path


# 不同模型表达同一个公式时的常见写法差异，归一化后就不该再当成"分歧"，
# 否则每次都要白白多花两次模型调用做仲裁。
_UNICODE_MATH = {
    "²": "^2", "³": "^3", "¹": "^1", "⁰": "^0", "⁴": "^4",
    "⁻": "-", "⁺": "+", "×": "*", "÷": "/", "·": "*",
    "＝": "=", "≠": "!=", "≤": "<=", "≥": ">=", "＜": "<", "＞": ">",
    "－": "-", "—": "-", "～": "~", "−": "-", "√": "\\sqrt",
    "（": "(", "）": ")", "，": ",", "。": ".", "：": ":", "；": ";",
}


_LATEX_ALIASES = {
    "\\dfrac": "\\frac", "\\tfrac": "\\frac", "\\cfrac": "\\frac",
    "\\cdot": "*", "\\times": "*", "\\div": "/",
    "\\le": "<=", "\\ge": ">=", "\\leq": "<=", "\\geq": ">=",
    "\\ne": "!=", "\\neq": "!=", "\\to": "->", "\\rightarrow": "->",
    "\\ldots": "...", "\\cdots": "...", "\\dots": "...",
    "\\,": "", "\\;": "", "\\!": "", "\\ ": "", "\\quad": "", "\\qquad": "",
    "\\left": "", "\\right": "", "\\displaystyle": "",
}


def _normalize_for_compare(text: str) -> str:
    """比较两次转写时忽略空格、引号、标点、上下标与 LaTeX 别名等无关差异。"""
    if not text:
        return ""
    t = text
    for src, dst in _UNICODE_MATH.items():
        t = t.replace(src, dst)
    # 必须带边界：否则 \le 会把 \left / \leftrightarrow 里的前缀吃掉
    for src, dst in _LATEX_ALIASES.items():
        t = re.sub(re.escape(src) + r"(?![A-Za-z])", dst.replace("\\", "\\\\"), t)
    t = re.sub(r"\s+", "", t)
    # 标点在题干里没有任何信息量，比对时全部去掉（半角 + 全角都算）
    t = re.sub(r"[\s＀-￯,.;:!?，。；：、！？…—·「」『』（）()\[\]{}]", "", t)
    for ch in ('“', '”', '"', "'", '‘', '’', "$"):
        t = t.replace(ch, "")
    return t


def sanitize_latex(text: str) -> str:
    """清掉模型常见的 LaTeX 毛刺：多余定界符、裸反斜杠、重复空行。"""
    if not text:
        return ""
    s = text.strip()
    s = re.sub(r"^```(?:latex|tex|math)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # 全角括号/标点混进公式时容易炸，统一成半角可解析的写法
    s = s.replace("（", "(").replace("）", ")")
    # 去掉成对的 $$ 包裹
    s = re.sub(r"^\$\$(.+)\$\$$", r"\1", s, flags=re.S)
    # 补齐花括号
    s = _balance_braces(s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _balance_braces(s: str) -> str:
    """逐字符扫描补齐未闭合的花括号（忽略被反斜杠转义的）。"""
    out: list[str] = []
    depth = 0
    escaped = False
    for ch in s:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(0, depth - 1)
        out.append(ch)
    if depth > 0:
        out.append("}" * depth)
    return "".join(out)


def transcribe_examples(
    kps: list[KnowledgePoint],
    candidates: dict[str, list[Path]],
    cues: list[Cue],
    video_title: str,
    shots_dir: Path,
    progress: Progress | None = None,
) -> None:
    """就地填充每道例题的题干、答案确认状态与最终截图。"""
    client = MiniMaxClient()
    all_ex = [(kp, ex) for kp in kps for ex in kp.examples]
    total = len(all_ex)
    ok_count = 0
    cross_checked = 0
    cross_mismatch = 0

    for idx, (kp, ex) in enumerate(all_ex, 1):
        frames = candidates.get(ex.id) or []
        if not frames:
            ex.verified = False
            ex.notes = _merge_notes(ex.notes, "⚠ 未能截取到画面")
            if progress:
                progress(idx / max(1, total), f"{ex.id} 没有可用截图，跳过转写")
            continue

        context = build_transcript_context(cues, ex.ts_start, ex.ts_end)
        prompt = TRANSCRIBE_USER.format(
            title=video_title or "教学视频",
            ts=fmt_ts(ex.ts_start),
            kp_title=kp.title,
            context=context[:1200] or "（无字幕上下文）",
        )

        # 第一遍：按优先级找第一张"确实是完整题干"的候选帧
        accepted = None
        rejects: list[str] = []
        failures = 0
        for frame in frames:
            data, err = _ask(client, prompt, frame)
            if data is None:
                failures += 1
                rejects.append(f"{frame.name}: {err}")
                continue
            has_problem = bool(data.get("has_problem"))
            conf = _as_conf(data.get("confidence"))
            statement = sanitize_latex(str(data.get("statement_latex") or ""))
            if has_problem and conf >= ACCEPT_CONFIDENCE and len(statement) >= 6:
                accepted = (data, frame, statement)
                break
            rejects.append(f"{frame.name}: " + ("画面里没有题目" if not has_problem else f"把握不足({conf:.2f})"))

        if accepted is None:
            ex.verified = False
            ex.shot = frames[0].name
            ex.shot_confidence = 0.0
            # 全是调用故障 vs 全是"图里没题"，要分开说，否则排查方向会完全跑偏
            if failures == len(frames):
                ex.notes = _merge_notes(
                    ex.notes,
                    f"⚠ 看图转写全部调用失败（{len(frames)} 张），不是画面问题：" + "；".join(rejects[:2]),
                )
            else:
                ex.notes = _merge_notes(ex.notes, "⚠ 未确认到完整题目：" + "；".join(rejects[:3]))
            if progress:
                progress(idx / max(1, total), f"⚠ {ex.id} 转写失败，建议人工看图")
            continue

        data, frame, statement = accepted
        cross_checked += 1

        # 第二遍：多模态有采样随机性，同一帧两次读也可能不同（尤其正负号）。
        # 用另一张候选帧独立转写一遍，对不上就仲裁。
        others = [f for f in frames if f != frame]
        if others:
            second = _ask(client, prompt, others[0])
            second_text = ""
            if isinstance(second, dict) and second.get("has_problem"):
                second_text = sanitize_latex(str(second.get("statement_latex") or ""))
            if second_text and _normalize_for_compare(second_text) != _normalize_for_compare(statement):
                cross_mismatch += 1
                resolved = _arbitrate(client, prompt, [frame, others[0]], statement, second_text)
                if resolved:
                    statement = resolved
                else:
                    ex.verified = False
                    ex.notes = _merge_notes(
                        ex.notes,
                        "⚠ 两帧转写不一致且无法仲裁，请人工核对：" + _diff_hint(statement, second_text),
                    )

        _apply(ex, data, frame, statement)
        if ex.verified:
            ok_count += 1
        if progress:
            mark = "✅" if ex.verified else "⚠"
            progress(
                idx / max(1, total),
                f"{mark} {ex.id}「{ex.title[:16]}」{'题干已转写' if ex.verified else '转写存疑，请人工核对'}",
            )

    if progress:
        progress(1.0, f"例题转写完成：{ok_count}/{total} 道确认成功，交叉核对 {cross_checked} 道、分歧 {cross_mismatch} 道")


def _ask(client, prompt: str, frame: Path) -> tuple[dict | None, str]:
    """返回 (解析结果, 原因)。失败时结果为 None，原因说明是真失败而非"没题目"。

    一定要区分这两种情况：调用失败（token 不够/网络/服务异常）和模型判定画面里没题目，
    混为一谈会把真实故障伪装成"这张图没题"，导致所有候选都被白白丢掉。
    """
    try:
        data = client.vision_json(prompt, [prepare_frame(frame)],
                                  temperature=0.0, max_tokens=VISION_MAX_TOKENS,
                                  label="transcribe")
    except LLMError as exc:
        return None, f"调用失败（{str(exc)[:110]}）"
    except Exception as exc:  # noqa: BLE001
        return None, f"调用异常（{type(exc).__name__}: {str(exc)[:90]}）"
    if not isinstance(data, dict):
        return None, "返回格式异常（不是 JSON 对象）"
    return data, ""


def _arbitrate(client, prompt: str, frames: list[Path], a: str, b: str) -> str | None:
    """把两张图一起给模型，让它逐字核对后给出定稿。失败就返回 None（交给人工）。"""
    question = (
        prompt
        + "\n\n【重要】上面两张图是同一道题在不同瞬间的画面，但逐字转写的结果不一致：\n"
        + f"版本一：{a[:400]}\n版本二：{b[:400]}\n\n"
        "请把两张图放在一起逐字核对（特别注意正负号、指数、小数点、题号、选项字母），"
        "给出你认为唯一正确的那一份题干。只输出 JSON，字段与前面要求一致。"
    )
    try:
        data = client.vision_json(question, [prepare_frame(f) for f in frames],
                                  temperature=0.0, max_tokens=6000, label="transcribe")
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict) or not data.get("has_problem"):
        return None
    text = sanitize_latex(str(data.get("statement_latex") or ""))
    return text if len(text) >= 6 else None


def _diff_hint(a: str, b: str) -> str:
    """指出两版在哪些字符上不同，方便老师一眼定位。"""
    import difflib

    na, nb = _normalize_for_compare(a), _normalize_for_compare(b)
    ops = [op for op in difflib.SequenceMatcher(None, na, nb).get_opcodes() if op[0] != "equal"]
    if not ops:
        return "（仅格式差异）"
    parts = []
    for tag, i1, i2, j1, j2 in ops[:3]:
        parts.append(f"{na[i1:i2] or '∅'} → {nb[j1:j2] or '∅'}")
    return "疑似差异：" + "，".join(parts)


def _apply(ex: Example, data: dict, frame: Path, statement: str) -> None:
    """把确认过的题干写进例题。"""
    conf = _as_conf(data.get("confidence"))
    ex.statement_latex = statement
    ex.statement_plain = str(data.get("statement_plain") or "").strip() or statement
    ex.shot = frame.name
    ex.shot_confidence = conf
    ex.verified = True

    label = str(data.get("problem_label") or "").strip()
    if label and label not in ex.title:
        ex.title = f"{ex.title}（{label}）"
    theme = str(data.get("problem_title") or "").strip()
    if theme and theme not in ex.title:
        ex.title = f"{ex.title}·{theme}"
    if data.get("contains_answer"):
        ex.notes = _merge_notes(ex.notes, "画面上已写出答案，解析按讲解补充")
    if data.get("illegible_parts"):
        ex.notes = _merge_notes(
            ex.notes, "⚠ 画面有不清处：" + "；".join(map(str, data["illegible_parts"]))
        )
    if data.get("has_diagram") and data.get("diagram_note"):
        ex.notes = _merge_notes(ex.notes, f"图形说明：{data['diagram_note']}")
        ex.statement_latex = _append_diagram(ex.statement_latex, str(data["diagram_note"]))


def _merge_notes(existing: str, addition: str) -> str:
    kept = [seg for seg in (existing or "").split(" | ") if seg and not seg.startswith(addition[:8])]
    kept.append(addition)
    return " | ".join(kept)[-600:]


def _append_diagram(statement: str, note: str) -> str:
    if "[图" in statement:
        return statement
    return f"{statement}\n[图示：{note}]"


def _as_conf(value) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0:  # 模型偶尔给百分制
        conf /= 100.0
    return max(0.0, min(1.0, conf))