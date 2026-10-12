# -*- coding: utf-8 -*-
"""自检：模型服务出问题时，必须还能交出一份教案。

实测故障（第 6 轮端到端）：review-global 遇到一次连接中断
（peer closed connection without sending complete message body），
紧接着 revise 连着 5 次 WinError 10061「目标计算机积极拒绝，无法连接」。
异常一路抛出 run_pipeline，整条流水线中断——前面十几分钟的转写、看图、
教案生成全部白做，最后一份 docx 都没产出。

老师宁可要一份"没回炉过、但附录里如实列着待核对项"的教案，
也不要什么都没有。这条钉的就是这件事。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.llm.client import LLMError, _is_connect_error, _is_retryable
from app.models import (Cue, Example, KnowledgePoint, Lesson, LessonMeta,
                        ReviewIssue, ReviewReport)
from app.stages import review as review_stage

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def make_lesson() -> Lesson:
    return Lesson(
        meta=LessonMeta(title="降级测试", subject="数学", grade="高一"),
        objectives=["目标一"],
        knowledge=[KnowledgePoint(id="kp1", title="考点一", definition="定义",
                                  summary="概述", examples=[
                                      Example(id="ex1", title="例题",
                                              statement_plain="题面", answer="答案")])],
    )


# ---------------------------------------------------------------- 连接类错误的判定与退避档位
CONN = [
    ConnectionRefusedError("[WinError 10061] 由于目标计算机积极拒绝，无法连接。"),
    ConnectionResetError("connection reset by peer"),
    Exception("peer closed connection without sending complete message body"),
    Exception("incomplete chunked read"),
]
for exc in CONN:
    check(f"识别为连接类错误：{str(exc)[:34]}…", _is_connect_error(exc))

check("普通业务错误不算连接类", not _is_connect_error(Exception("模型返回空内容")),
      "空内容靠退避重试没用，别浪费 45 秒")
check("过载错误不算连接类", not _is_connect_error(LLMError("[529] 服务集群负载较高")))
check("None 安全", _is_connect_error(None) is False)
check("连接类错误仍然可重试", _is_retryable(0, ConnectionRefusedError("10061")))

# ---------------------------------------------------------------- 回炉失败必须降级出稿
GOOD = make_lesson()
REPORT = ReviewReport(
    verdict="revise", score=70, rounds=0, model_review_ok=True,
    summary="两处公式符号笔误需要修正。",
    issues=[ReviewIssue(kind="fact", severity="major", target="ex1",
                        problem="答案写错了", suggestion="改成正确值")],
)

calls = {"review": 0, "refine": 0}


def fake_review(lesson, cues, shots_dir, duration, progress=None, **kw):
    calls["review"] += 1
    return REPORT.model_copy(deep=True)


def fake_refine(lesson, report, cues, shots_dir, duration, round_no, progress=None, **kw):
    calls["refine"] += 1
    raise LLMError(
        "MiniMax-M3/MiniMax-M3 调用失败（revise-1/3，试了 5 次）："
        "最后一次错误：[WinError 10061] 由于目标计算机积极拒绝，无法连接。"
    )


saved = {"n": 0}


def fake_on_lesson(lesson):
    saved["n"] += 1


review_stage.review = fake_review
review_stage.refine = fake_refine

try:
    out = review_stage.run_review_loop(
        GOOD, [], Path("shots"), 360.0, max_rounds=3,
        on_lesson=fake_on_lesson,
    )
finally:
    pass

print(f"  回炉被调用 {calls['refine']} 次，落盘 {saved['n']} 次")
check("回炉失败后仍然返回一份完整的 Lesson", isinstance(out, Lesson))
check("知识点一条没少", len(out.knowledge) == 1 and out.knowledge[0].id == "kp1")
check("例题一条没少", len(out.knowledge[0].examples) == 1)
check("审核意见仍然挂在教案上", len(out.review.issues) == 1)
check("结论降级为需人工复核", out.review.verdict == "revise", out.review.verdict)
check("如实写明回炉没做成",
      "回炉修订未能完成" in out.review.summary, out.review.summary[-70:])
check("待核对项条数如实交代", "1 条待核对项" in out.review.summary)
check("统计里标记了回炉失败", out.stats.get("review_refine_failed") is True,
      str(out.stats))
check("回炉次数如实记 0（这一轮没做成）", out.stats.get("review_rounds") == 0,
      str(out.stats.get("review_rounds")))
check("失败后立刻收手，不再烧第二轮", calls["refine"] == 1, f"调了 {calls['refine']} 次")
check("中间结果至少落盘过一次", saved["n"] >= 1, f"落盘 {saved['n']} 次")

# ---------------------------------------------------------------- 没有事实错误就不该进回炉
SUP_ONLY = ReviewReport(
    verdict="revise", score=80, rounds=0, model_review_ok=True, summary="建议补一句口诀。",
    issues=[ReviewIssue(kind="supplement", severity="minor", target="kp1",
                        problem="可以再讲细一点", suggestion="补个例子")],
)
review_stage.review = lambda *a, **k: SUP_ONLY.model_copy(deep=True)
review_stage.refine = fake_refine
out2 = review_stage.run_review_loop(make_lesson(), [], Path("shots"), 360.0, max_rounds=3)
check("只有教学补充时根本不进回炉", calls["refine"] == 1,
      f"回炉调用次数仍是 {calls['refine']}（没增加就对了）")
check("教学补充也算跑完了，不算失败", out2.stats.get("review_refine_failed") is None)

# ---------------------------------------------------------------- 过期意见必须在判定前剔除
# 实测第 7 轮：16 条事实错误里 12 条属于 ex1/ex2/ex3 这三条**已被降级成口述要点**
# 的条目，它们根本不在教案里了。后果是判定永远 fail、还白白烧掉一整轮回炉。
from app.stages.review import _drop_stale_issues

mixed = Lesson(
    meta=LessonMeta(title="过期意见测试"),
    knowledge=[
        KnowledgePoint(id="kp1", title="考点一", examples=[
            Example(id="ex1", title="口述要点", statement_plain="", is_exercise=False),
            Example(id="ex2", title="口述要点", statement_plain="", is_exercise=False),
            Example(id="ex3", title="口述要点", statement_plain="", is_exercise=False),
            Example(id="ex4", title="真例题", statement_plain="题面", is_exercise=True)]),
    ],
)
mixed.review.issues = [
    ReviewIssue(kind="fact", severity="major", target="ex1", problem="ex1 缺少题目截图"),
    ReviewIssue(kind="fact", severity="major", target="ex2", problem="ex2 题干可能不准"),
    ReviewIssue(kind="fact", severity="major", target="ex4", problem="ex4 答案写错了"),
    ReviewIssue(kind="fact", severity="minor", target="整体", problem="缺少课后作业"),
    # 点名多道题但还剩一道真例题 —— 属于跨条目的整体判断，必须留着
    ReviewIssue(kind="fact", severity="minor", target="ex1, ex4",
                problem="两条的术语不统一"),
]
gone = _drop_stale_issues(mixed.review, mixed)
left = [i.target for i in mixed.review.issues]
print(f"  剔除 {gone} 条，剩 {left}")
check("针对已降级例题的意见被剔除", gone == 2, f"剔除了 {gone} 条")
check("真例题的意见留着", "ex4" in left)
check("整体性意见留着", "整体" in left)
check("跨条目的意见留着（还剩一道真例题）", "ex1, ex4" in left, str(left))
check("没有降级条目时不动任何意见", _drop_stale_issues(mixed.review, make_lesson()) == 0
      if all(e.is_exercise for k in make_lesson().knowledge for e in k.examples) else True)
empty = Lesson(meta=LessonMeta(title="t"), knowledge=[])
check("没有知识点时安全", _drop_stale_issues(empty.review, empty) == 0)

# ---------------------------------------------------------------- 空作业不算缺陷
# 作业为空是**设计如此**：视频里老师没布置作业，而我们有防编造闸门，
# 模型自己出的题一律毙掉。以前照实报成"缺少课后作业"，于是每一份教案都必然
# fail——这条规则和防编造闸门互相打架，把判定锁死在过不了关。
from app.models import Cue
from app.stages.review import rule_check


def _hw_lesson(transcript: str, homework: list[str]):
    L = Lesson(
        meta=LessonMeta(title="t", video_duration=60.0),
        objectives=["目标一", "目标二", "目标三"],
        key_points=["重点"], difficulties=["难点"], materials=["粉笔"],
        common_mistakes=["易错"], flow=[{"stage": "导入", "minutes": 5}],
        homework=homework,
        knowledge=[KnowledgePoint(id="kp1", title="考点", summary="概述",
                                  key_points=["要点"], ts_start=0.0, ts_end=60.0,
                                  examples=[Example(id="ex1", title="题",
                                                    statement_plain="题面",
                                                    verified=True,
                                                    is_exercise=True)])],
    )
    cues = [Cue(start=i * 2.0, end=i * 2.0 + 2.0, text=t)
            for i, t in enumerate(transcript.split("。") if transcript else [])]
    return L, cues


_CLEAN = "今天讲倾斜角的定义。范围是零到π。斜率等于正切值。"
_a, _ca = _hw_lesson(_CLEAN, [])
check("视频里没布置作业 -> 空作业不算缺陷",
      not [x for x in rule_check(_a, Path("shots"), 60.0, _ca) if "课后作业" in x])
_b, _cb = _hw_lesson(_CLEAN + "。最后布置一下课后作业，课本第 52 页。", [])
check("视频里真布置了作业却没列 -> 仍然是缺陷",
      any("课后作业" in x for x in rule_check(_b, Path("shots"), 60.0, _cb)))
_c, _cc = _hw_lesson(_CLEAN, ["1. 完成课本练习"])
check("本来就列了作业 -> 不报",
      not [x for x in rule_check(_c, Path("shots"), 60.0, _cc) if "课后作业" in x])
_d, _cd = _hw_lesson("", [])
check("没有字幕时的空作业也安全",
      not [x for x in rule_check(_d, Path("shots"), 60.0, _cd) if "课后作业" in x])

# ---------------------------------------------------------------- 骨架空壳要自动重试
# 实测第 8 轮：compose-outline 返回空壳后直接抛异常中止，
# 二十分钟的 ASR/切分/抽帧/看图全部白做。跟审核阶段那次是同一类毛病。
from app.models import KnowledgePoint as _KP
from app.stages.compose import OUTLINE_RETRIES, _outline_degenerate

_good = {"knowledge": [
    {"id": "kp1", "summary": "讲了定义", "definition": "定义"},
    {"id": "kp2", "summary": "讲了推导", "definition": "推导"},
]}
print(f"  OUTLINE_RETRIES = {OUTLINE_RETRIES}")
check("有内容的骨架不判空壳", not _outline_degenerate(_good, [_KP(id="kp1", title="a")]))
check("全空的骨架判空壳", _outline_degenerate({"knowledge": [
    {"id": "kp1", "summary": "", "definition": ""},
    {"id": "kp2", "summary": "", "definition": ""}]}, []))
check("knowledge 缺失判空壳", _outline_degenerate({}, []))
check("knowledge 不是列表判空壳", _outline_degenerate({"knowledge": "x"}, []))
check("返回的不是 dict 也判空壳", _outline_degenerate(["kp1"], []))
check("一半有内容仍算塌（宁严勿松）", _outline_degenerate({"knowledge": [
    {"id": "kp1", "summary": "有", "definition": "有"},
    {"id": "kp2", "summary": "", "definition": ""},
    {"id": "kp3", "summary": "", "definition": ""},
    {"id": "kp4", "summary": "", "definition": ""}]}, []))
check("给了重试机会", OUTLINE_RETRIES >= 2, f"{OUTLINE_RETRIES} 次")

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails[:6]))
    raise SystemExit(1)
print("✅ 模型服务出问题时，仍然交得出教案")
