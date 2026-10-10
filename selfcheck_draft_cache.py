"""断点保存自检：崩溃/休眠之后重跑，不能把已经算出来的东西再算一遍。

背景：compose 要跑十来分钟、审核二十几分钟，中间结果全在内存里。
机器睡一觉或者进程被杀，前面一小时全白跑——已经连着发生两次。
这里锁死两件事：草稿能读回来、每轮回炉后都会落盘。
"""
import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint, Lesson, LessonMeta, ReviewIssue, ReviewReport  # noqa: E402
from app.pipeline import _load_draft  # noqa: E402
from app.stages import compose as compose_stage  # noqa: E402
from app.stages import review as rv  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def sample_lesson(subject="数学", grade="高考", dur=90.0) -> Lesson:
    lesson = Lesson()
    lesson.meta = LessonMeta(title="图像与比大小", subject=subject, grade=grade, duration_min=dur)
    kp = KnowledgePoint(id="kp1", title="知识点一", ts_start=0.0, ts_end=600.0,
                        summary="讲图像平移", definition="左移即 x+a", formula_latex="y=f(x+a)")
    kp.examples.append(Example(id="ex1", title="例1", ts_start=10.0,
                               statement_latex="左移 2 个单位", answer="y=(x+2)^2",
                               solution=["左移即 x+2"]))
    lesson.knowledge.append(kp)
    return lesson


print("=== 用例 1：草稿能读回来 ===")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "lesson.draft.json"
    want = LessonMeta(title="图像与比大小", subject="数学", grade="高考", duration_min=90.0)

    check("文件不存在时返回 None", _load_draft(p, want, True, None) is None)

    compose_stage.save(sample_lesson(), p)
    got = _load_draft(p, want, True, None)
    check("能读回上一版教案", got is not None and len(got.knowledge) == 1)
    check("内容没丢：公式", got.knowledge[0].formula_latex == "y=f(x+a)")
    check("内容没丢：例题答案", got.knowledge[0].examples[0].answer == "y=(x+2)^2")
    check("内容没丢：解析", got.knowledge[0].examples[0].solution == ["左移即 x+2"])
    check("reuse=False 不读缓存", _load_draft(p, want, False, None) is None)

    print()
    print("=== 用例 2：条件对不上就作废 ===")
    # 这三个字段直接进教案 meta 和顶层内容，沿用旧稿等于交一份对不上的东西
    check("学科不同 → 作废",
          _load_draft(p, want.model_copy(update={"subject": "物理"}), True, None) is None)
    check("年级不同 → 作废",
          _load_draft(p, want.model_copy(update={"grade": "初一"}), True, None) is None)
    check("时长不同 → 作废",
          _load_draft(p, want.model_copy(update={"duration_min": 45.0}), True, None) is None)
    check("时长差 0.01 也算不同（宁可重算）",
          _load_draft(p, want.model_copy(update={"duration_min": 90.01}), True, None) is None)
    check("完全一致 → 仍然复用", _load_draft(p, want, True, None) is not None)

    print()
    print("=== 用例 3：坏文件不能让整条流水线挂掉 ===")
    p.write_text("{ 这不是合法 JSON", encoding="utf-8")
    check("损坏的 JSON → 返回 None 而不是抛异常", _load_draft(p, want, True, None) is None)
    p.write_text('{"knowledge": []}', encoding="utf-8")
    check("空教案 → 不复用", _load_draft(p, want, True, None) is None)
    p.write_text('{"meta": {}, "knowledge": [{"id": "kp1"}]}', encoding="utf-8")
    # meta 是空的、和请求对不上，返回 None 是对的；这里要验的是"别抛异常"
    try:
        r = _load_draft(p, want, True, None)
        check("字段缺失也不抛异常", r is None or len(r.knowledge) == 1)
    except Exception as exc:  # noqa: BLE001
        check("字段缺失也不抛异常", False, f"{type(exc).__name__}: {exc}")

print()
print("=== 用例 4：每轮回炉后都要落盘 ===")
sig = inspect.signature(rv.run_review_loop)
check("run_review_loop 有 on_lesson 回调", "on_lesson" in sig.parameters)
check("回调是可选的，不传也不炸",
      sig.parameters["on_lesson"].default is None)

# 用假的 review / refine 把循环走完，数回调次数
saved: list[int] = []
at_first_refine: list[int] = []
real_review, real_refine = rv.review, rv.refine
rounds = {"n": 0}


def fake_review(lesson, cues, shots_dir, duration, progress=None):
    rounds["n"] += 1
    if rounds["n"] > 3:
        return ReviewReport(verdict="pass", model_review_ok=True, score=95)
    # 每轮少一条：这是"回炉有效果"的场景，用来验落盘次数
    n = max(1, 4 - rounds["n"])
    return ReviewReport(verdict="revise", model_review_ok=True,
                        issues=[ReviewIssue(severity="major", target="kp1", problem="改一下")
                                for _ in range(n)])


def fake_refine(lesson, report, cues, shots_dir, duration, round_no, progress=None):
    at_first_refine.append(len(saved))  # 记录第一次回炉时已经存过几次
    lesson = lesson.model_copy(deep=True)
    lesson.knowledge[0].summary = f"第{round_no}轮改过"
    return lesson, fake_review(lesson, cues, shots_dir, duration, progress)


rv.review, rv.refine = fake_review, fake_refine
try:
    out = rv.run_review_loop(sample_lesson(), [], Path("."), 2821.0,
                             max_rounds=2, on_lesson=lambda ls: saved.append(len(ls.knowledge)))
finally:
    rv.review, rv.refine = real_review, real_refine

check("初次审核后就落盘了一次（回炉前就有存档）",
      at_first_refine and at_first_refine[0] == 1, f"第一次回炉时已存 {at_first_refine}")
check("两轮回炉各落盘一次，共 3 次", len(saved) == 3, f"存了 {len(saved)} 次")
check("两轮回炉后确实是最新版本", out.knowledge[0].summary == "第2轮改过")
check("回炉轮数记进 stats", out.stats.get("review_rounds") == 2, f"rounds={out.stats.get('review_rounds')}")

print()
print("=== 用例 5：不传回调也能正常跑（老调用方不受影响）===")
rv.review, rv.refine = fake_review, fake_refine
try:
    out2 = rv.run_review_loop(sample_lesson(), [], Path("."), 2821.0, max_rounds=1)
finally:
    rv.review, rv.refine = real_review, real_refine
check("不传 on_lesson 不报错", out2 is not None and len(out2.knowledge) == 1)

print()
print("=== 用例 6：回炉没效果就早停，别白烧一轮 ===")
refine_calls: list[int] = []


def stuck_review(lesson, cues, shots_dir, duration, progress=None):
    # 永远是 35 条——实测跑出来就是这个形状：改完还是 35 条
    return ReviewReport(verdict="fail", model_review_ok=True, score=60,
                        issues=[ReviewIssue(severity="major", target=f"kp{i}", problem="同一类问题")
                                for i in range(35)])


def stuck_refine(lesson, report, cues, shots_dir, duration, round_no, progress=None):
    refine_calls.append(round_no)
    return lesson.model_copy(deep=True), stuck_review(lesson, cues, shots_dir, duration, progress)


rv.review, rv.refine = stuck_review, stuck_refine
try:
    stalled = rv.run_review_loop(sample_lesson(), [], Path("."), 2821.0, max_rounds=2)
finally:
    rv.review, rv.refine = real_review, real_refine

check("问题不降时只回炉 1 次，不跑满 2 轮", refine_calls == [1], f"实际回炉 {len(refine_calls)} 次")
check("rounds 如实记 1", stalled.stats.get("review_rounds") == 1,
      f"rounds={stalled.stats.get('review_rounds')}")
check("标了 stalled", stalled.stats.get("review_stalled") is True)
check("结论文案说明了为什么停", "没有减少" in stalled.review.summary)
check("没有再套一句『已回炉 2 轮仍未通过』", "已回炉 2 轮" not in stalled.review.summary)

print()
print("=== 用例 7：问题确实在降就照常跑满轮数 ===")
calls: list[int] = []
real_review2, real_refine2 = rv.review, rv.refine


def dropping_review(lesson, cues, shots_dir, duration, progress=None):
    # 3→2→1→0，第 3 轮回炉后收敛，且每轮都在明显下降（不会被早停掐断）
    n = 3 - len(calls)
    if n <= 0:
        return ReviewReport(verdict="pass", model_review_ok=True, score=92)
    return ReviewReport(verdict="revise", model_review_ok=True,
                        issues=[ReviewIssue(severity="minor", target="kp1", problem="小问题")
                                for _ in range(n)])


def dropping_refine(lesson, report, cues, shots_dir, duration, round_no, progress=None):
    calls.append(round_no)
    return lesson.model_copy(deep=True), dropping_review(lesson, cues, shots_dir, duration, progress)


rv.review, rv.refine = dropping_review, dropping_refine
try:
    okrun = rv.run_review_loop(sample_lesson(), [], Path("."), 2821.0, max_rounds=3)
finally:
    rv.review, rv.refine = real_review2, real_refine2

check("有效果时不被提前掐断", len(calls) >= 2, f"实际回炉 {len(calls)} 次")
check("收敛后判 pass", okrun.review.verdict == "pass", f"verdict={okrun.review.verdict}")
check("没有误标 stalled", not okrun.stats.get("review_stalled"))

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)