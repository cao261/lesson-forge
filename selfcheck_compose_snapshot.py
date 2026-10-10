"""自检：compose 的中途存档绝不能把主流程带崩。

踩过的坑：on_partial 里构造 Lesson 抛了 LLMError，异常一路冒到
run_batches 的 worker，导致 5 批例题全跑完（十几分钟算力）却整轮失败。
存档是锦上添花，失败就必须安静吞掉。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.llm.client import LLMError
from app.models import Cue, Example, KnowledgePoint, LessonMeta
from app.stages import compose

CASES: list[tuple[str, bool, str]] = []


def expect(name: str, cond: bool, detail: str = "") -> None:
    CASES.append((name, cond, detail))


def make_kps() -> list[KnowledgePoint]:
    """带例题的知识点——_to_lesson 的例题是从原始 kps 里按 id 取的，
    这里不给 examples 的话，合并进去的例题会被丢掉（是测试数据的坑）。"""
    return [
        KnowledgePoint(
            id=f"kp{i}", title=f"知识点{i}",
            ts_start=float(i * 10), ts_end=float(i * 10 + 9),
            examples=[Example(id=f"e{i}", statement_latex=f"${i}$", title=f"例{i}")],
        )
        for i in range(1, 4)
    ]


def make_meta() -> LessonMeta:
    return LessonMeta(url="https://x/y", title="T", uploader="U",
                      video_duration=100.0, webpage_url="https://x/y")


# ---- 1. _merge_examples：按 kp id 挂回 ----
skeleton = {
    "knowledge": [
        {"id": "kp1", "summary": "s1"},
        {"id": "kp2", "summary": "s2"},
    ]
}
ex_map = {"kp1": [{"id": "e1", "answer": "42"}]}
merged = compose._merge_examples(skeleton, ex_map)
rows = {r["id"]: r for r in merged["knowledge"]}
expect("例题按 kp id 挂回骨架",
       rows["kp1"].get("examples") == [{"id": "e1", "answer": "42"}], str(rows))
expect("没有例题的知识点不受影响", not rows["kp2"].get("examples"), str(rows["kp2"]))

# ---- 2. 空 ex_map 时原样返回 ----
same = compose._merge_examples(skeleton, {})
expect("ex_map 为空时原样返回", same.get("knowledge") == skeleton["knowledge"])

# ---- 3. 非 dict 输入不炸 ----
expect("输入不是 dict 时返回空 dict", compose._merge_examples(None, ex_map) == {})
expect("输入不是 dict 且 ex_map 空时也安全",
       compose._merge_examples("垃圾", {}) == {})

# ---- 4. 存档回调抛异常时，compose 不能因此失败 ----
# 直接验证 _snapshot 的兜底：让 on_partial 抛 LLMError，
# 用一次真实的 compose 调用（打桩掉模型调用）来确认主流程照常返回。
calls: list[str] = []


class _Boom(Exception):
    pass


def _bad_on_partial(lesson):
    calls.append("save")
    raise LLMError("存档故意失败")


# 用最小骨架验证：_to_lesson 对这份数据能构造出非空 Lesson
data = {
    "meta": {"title": "教案", "subject": "数学", "grade": "高一", "duration_min": 47.0},
    "objectives": ["目标一", "目标二"],
    "key_points": ["重点"],
    "knowledge": [
        {"id": "kp1", "title": "知识点一", "summary": "摘要一", "definition": "定义一"},
        {"id": "kp2", "title": "知识点二", "summary": "摘要二", "definition": "定义二"},
        {"id": "kp3", "title": "知识点三", "summary": "摘要三", "definition": "定义三"},
    ],
    "flow": [{"stage": "导入", "duration_min": 5.0, "activity": "复习"}],
}
merged_full = compose._merge_examples(
    data,
    {
        "kp1": [{"id": "e1", "statement_latex": "$x$", "answer": "1", "solution": ["s"]}],
        "kp2": [{"id": "e2", "statement_latex": "$y$", "answer": "2", "solution": ["s"]}],
        "kp3": [{"id": "e3", "statement_latex": "$z$", "answer": "3", "solution": ["s"]}],
    },
)
lesson_ok = compose._to_lesson(merged_full, make_meta(), make_kps(), 47.0)
expect("完整数据能构造出非空 Lesson",
       lesson_ok.knowledge and lesson_ok.example_count() == 3,
       f"知识点 {len(lesson_ok.knowledge)} 例题 {lesson_ok.example_count()}")
expect("完整 Lesson 不被判为空壳", compose._degenerate(lesson_ok) == "",
       compose._degenerate(lesson_ok))

# 部分草稿（有骨架、只挂上部分例题）也要能构造出来，否则存档白做
merged_part = compose._merge_examples(
    data,
    {"kp1": [{"id": "e1", "statement_latex": "$x$", "answer": "1", "solution": ["s"]}]},
)
lesson_part = compose._to_lesson(merged_part, make_meta(), make_kps(), 47.0)
expect("部分草稿也能构造出 Lesson", len(lesson_part.knowledge) == 3)
expect("部分草稿同样不是空壳", compose._degenerate(lesson_part) == "",
       compose._degenerate(lesson_part))

# ---- 5. compose 里 on_partial 抛错不影响返回 ----
# 打桩：让 chat_object 依次返回骨架和例题批
import app.stages.compose as C

seq = [
    # 第一发：骨架
    {
        "meta": {"title": "教案", "subject": "数学", "grade": "高一", "duration_min": 47.0},
        "objectives": ["目标一"],
        "knowledge": [
            {"id": "kp1", "title": "知识点一", "summary": "s1", "definition": "d1",
             "examples": [{"id": "e1", "statement_latex": "$x$"}]},
            {"id": "kp2", "title": "知识点二", "summary": "s2", "definition": "d2",
             "examples": [{"id": "e2", "statement_latex": "$y$"}]},
            {"id": "kp3", "title": "知识点三", "summary": "s3", "definition": "d3",
             "examples": [{"id": "e3", "statement_latex": "$z$"}]},
        ],
        "flow": [{"stage": "导入", "duration_min": 5.0, "activity": "复习"}],
    },
    # 后面每一发：例题批
    {"knowledge": [
        {"id": "kp1", "examples": [{"id": "e1", "statement_latex": "$x$",
                                   "answer": "1", "solution": ["解"]}]},
        {"id": "kp2", "examples": [{"id": "e2", "statement_latex": "$y$",
                                   "answer": "2", "solution": ["解"]}]},
    ]},
    {"knowledge": [
        {"id": "kp3", "examples": [{"id": "e3", "statement_latex": "$z$",
                                   "answer": "3", "solution": ["解"]}]},
    ]},
]

orig_chat = C.MiniMaxClient.chat_object


def fake_chat(self, messages, **kw):
    return seq.pop(0) if seq else {"knowledge": []}


C.MiniMaxClient.chat_object = fake_chat
try:
    out = compose.compose(
        make_meta(), make_kps(), [Cue(start=0.0, end=1.0, text="字幕")], 47.0,
        None, on_partial=_bad_on_partial,
    )
    expect("on_partial 一直抛异常时 compose 仍正常返回",
           out is not None and len(out.knowledge) == 3,
           f"knowledge={len(out.knowledge) if out else None}")
    expect("存档回调确实被调用过（说明测试有效）", len(calls) >= 1, f"调用 {len(calls)} 次")
except Exception as exc:  # noqa: BLE001
    import traceback
    expect("on_partial 一直抛异常时 compose 仍正常返回", False,
           f"抛了 {type(exc).__name__}: {str(exc)[:160]}\n"
           + "".join(traceback.format_tb(exc.__traceback__)[-3:]))
finally:
    C.MiniMaxClient.chat_object = orig_chat

ok = sum(1 for _, p, _ in CASES if p)
bad = len(CASES) - ok
for name, passed, detail in CASES:
    print(f"  {'✅' if passed else '❌'} {name}" + (f"  →  {detail}" if not passed else ""))
print(f"\n自检 {ok}/{len(CASES)} 通过")
sys.exit(0 if bad == 0 else 1)