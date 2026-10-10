"""离线假数据模式（LESSONFORGE_MOCK=1）。

用途：在没有 API Key 的情况下把「下载→转写→截图→组教案→审核→导出 Word」整条链路跑通，
用来验证环境和代码，不会产出真正有内容的教案。
"""
from __future__ import annotations

import json
import re
from typing import Any

TS_RE = re.compile(r"\[(\d{2}):(\d{2})(?::(\d{2}))?\]")


def _fmt(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _transcript_cues(text: str) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for m in TS_RE.finditer(text):
        mm, ss, hh = m.group(1), m.group(2), m.group(3)
        t = int(hh) * 3600 + int(mm) * 60 + int(ss) if hh else int(mm) * 60 + int(ss)
        body = text[m.end() : text.find("\n", m.end()) if text.find("\n", m.end()) != -1 else len(text)]
        body = body.strip()
        if body:
            out.append((float(t), body))
    return out


def _mock_segment(user_text: str) -> dict[str, Any]:
    cues = _transcript_cues(user_text)
    if not cues:
        cues = [(0.0, "（假数据模式：没有真实字幕）")]
    chunk = max(1, len(cues) // 4)
    segments = []
    for i in range(0, len(cues), chunk):
        part = cues[i : i + chunk]
        start, end = part[0][0], part[-1][0]
        quotes = [f"[{_fmt(t)}] {x}" for t, x in part[:2]]
        has_ex = i + chunk < len(cues)
        examples = (
            [
                {
                    "title": "例1",
                    "ts_start": end,
                    "hint": "（假数据）例题大意",
                    "cue": quotes[-1],
                }
            ]
            if has_ex
            else []
        )
        segments.append(
            {
                "title": f"知识点 {i // chunk + 1}",
                "ts_start": round(start, 1),
                "ts_end": round(end, 1),
                "summary": "（假数据模式）这一段的大意：" + " ".join(x for _, x in part[:4]),
                "definition": "（假数据模式）定义占位",
                "formula_latex": "$a^2 + b^2 = c^2$" if i == 0 else "",
                "key_points": ["（假数据）要点一", "（假数据）要点二"],
                "pitfalls": ["（假数据）易错点"],
                "source_quote": " ".join(quotes),
                "examples": examples,
            }
        )
    return {
        "topic": "假数据课程",
        "subject": "数学",
        "grade": "高一",
        "segments": segments,
    }


def _mock_transcribe(user_text: str) -> dict[str, Any]:
    return {
        "has_problem": True,
        "confidence": 0.5,
        "problem_label": "例1",
        "problem_title": "假数据题目",
        "statement_latex": "（假数据模式）已知 $x=1$，求 $y$ 的值。",
        "statement_plain": "（假数据模式）已知 x 等于 1，求 y 的值。",
        "has_diagram": False,
        "diagram_note": "",
        "contains_answer": False,
        "illegible_parts": [],
    }


def _mock_compose(user_text: str) -> dict[str, Any]:
    """从提示词里的知识点块反推出骨架，保证整条链路的 id/shot 对得上。"""
    kp_ids = re.findall(r"^### (\S+)｜(.+?)\s+\[", user_text, flags=re.M)
    ex_rows = re.findall(
        r"id=(\S+)｜(.+?)｜时间戳 (\S+)–(\S+)｜截图文件=(\S+)", user_text
    )
    examples_by_kp: dict[str, list[dict[str, Any]]] = {}
    current_kp = kp_ids[0][0] if kp_ids else "kp1"
    for line in user_text.splitlines():
        kp_match = re.match(r"^### (\S+)｜", line)
        if kp_match:
            current_kp = kp_match.group(1)
        ex_match = re.match(r"\s+- id=(\S+)｜(.+?)｜时间戳", line)
        if ex_match:
            examples_by_kp.setdefault(current_kp, []).append(
                {
                    "id": ex_match.group(1),
                    "title": ex_match.group(2),
                    "ts_start": 0.0,
                    "statement_latex": "（假数据模式）题干占位",
                    "statement_plain": "（假数据模式）题干占位",
                    "answer": "（假数据模式）答案占位",
                    "solution": ["（假数据模式）第一步", "（假数据模式）第二步"],
                    "difficulty": "中等",
                    "tags": ["（假数据）考点"],
                    "shot": "",
                    "notes": "",
                }
            )
    for ex_id, _title, ts, _ts2, shot in ex_rows:
        for items in examples_by_kp.values():
            for item in items:
                if item["id"] == ex_id:
                    item["shot"] = shot

    knowledge = []
    for idx, (kp_id, kp_title) in enumerate(kp_ids, 1):
        knowledge.append(
            {
                "id": kp_id,
                "title": kp_title,
                "ts_start": 0.0,
                "ts_end": 0.0,
                "summary": "（假数据模式）知识点摘要",
                "definition": "（假数据模式）定义",
                "formula_latex": "$E = mc^2$" if idx == 1 else "",
                "key_points": ["（假数据）要点"],
                "pitfalls": ["（假数据）易错点"],
                "teaching_notes": "（假数据模式）讲法建议",
                "source_quote": "（假数据模式）字幕原句",
                "examples": examples_by_kp.get(kp_id, []),
            }
        )

    return {
        "meta": {"title": "假数据教案", "subject": "数学", "grade": "高一", "duration_min": 60},
        "objectives": ["（假数据）目标一", "（假数据）目标二", "（假数据）目标三"],
        "key_points": ["（假数据）重点"],
        "difficulties": ["（假数据）难点"],
        "prerequisites": ["（假数据）前置知识"],
        "materials": ["（假数据）教具"],
        "knowledge": knowledge,
        "flow": [
            {
                "stage": "导入",
                "duration_min": 5.0,
                "activity": "（假数据）活动",
                "teacher_script": "（假数据）话术",
                "intent": "（假数据）意图",
            }
        ],
        "common_mistakes": ["（假数据）易错点"],
        "homework": ["（假数据）作业一"],
    }


def _mock_review(user_text: str) -> dict[str, Any]:
    return {
        "verdict": "pass",
        "score": 80,
        "hallucination_risk": "low",
        "summary": "假数据模式：审核通过（未做真实核查）。",
        "issues": [],
    }


def _mock_revise(user_text: str) -> dict[str, Any]:
    return _mock_compose(user_text.split("## 审核意见")[0])


_DISPATCH = {
    "segment": _mock_segment,
    "transcribe": _mock_transcribe,
    "compose": _mock_compose,
    "review": _mock_review,
    "revise": _mock_revise,
}


def mock_chat(messages: list[dict[str, Any]], label: str = "llm") -> str:
    handler = _DISPATCH.get(label, _mock_segment)
    user_text = ""
    for msg in reversed(messages):
        content = msg.get("content")
        if isinstance(content, str):
            user_text = content
            break
        if isinstance(content, list):
            user_text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
            break
    return json.dumps(handler(user_text), ensure_ascii=False)