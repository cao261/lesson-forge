"""模型审核分批自检：review 不能再像 revise 那样一次吞掉整份教案。

背景：整份教案 + 全量字幕 + 11 个知识点一次丢给模型，review 连撞两次 600s
超时，任务卡死。改成按知识点分批评 + 一次只看顶层字段的全局核查。
这里锁死两件事：常量存在、裁剪真的裁干净了。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint, Lesson  # noqa: E402
from app.stages import review as rv  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def make_lesson(n: int = 11) -> Lesson:
    lesson = Lesson()
    lesson.meta.title = "图像与比大小"
    lesson.objectives = ["掌握图像变换", "会比较大小"]
    lesson.key_points = ["数形结合"]
    lesson.homework = ["课后练习 1"]
    for i in range(1, n + 1):
        kp = KnowledgePoint(
            id=f"kp{i}", title=f"知识点{i}", ts_start=float(i * 60), ts_end=float(i * 60 + 50),
            summary=f"第 {i} 段讲的是图像变换", definition=f"定义 {i}",
            formula_latex="y=f(x)", key_points=[f"要点{i}"],
        )
        kp.board_shot = f"kp{i}_board.png"
        kp.examples.append(Example(
            id=f"ex{i}", title=f"例{i}", ts_start=float(i * 60 + 10),
            statement_latex="比较 $a$ 与 $b$ 的大小", statement_plain="比较 a 与 b",
            answer="a<b", solution=["因为 a<0"], shot=f"ex{i}.png", verified=True,
            shot_confidence=0.9,
        ))
        lesson.knowledge.append(kp)
    return lesson


print("=== 用例 1：REVIEW_BATCH 必须真的存在 ===")
# 这条对应今天那个静默 bug：文件里用了 REVIEW_BATCH 却没定义，
# 编译能过，跑起来才 NameError。
check("常量已定义", hasattr(rv, "REVIEW_BATCH"))
check("是正整数", isinstance(getattr(rv, "REVIEW_BATCH", 0), int)
      and getattr(rv, "REVIEW_BATCH", 0) > 0, f"值={getattr(rv, 'REVIEW_BATCH', '缺失')}")
check("批大小不至于大到等于没拆", getattr(rv, "REVIEW_BATCH", 99) <= 5,
      f"值={getattr(rv, 'REVIEW_BATCH', '缺失')}")

print()
print("=== 用例 2：分批数学对 ===")
B = rv.REVIEW_BATCH
lesson = make_lesson(11)
kps = list(lesson.knowledge)
batches = [kps[i:i + B] for i in range(0, len(kps), B)]
check("11 个知识点被拆开", len(batches) > 1, f"{len(kps)} 个 -> {len(batches)} 批")
check("批数正确", len(batches) == -(-11 // B), f"期望 {-(-11 // B)} 批")
check("每个知识点只出现在一批里",
      sorted(k.id for b in batches for k in b) == sorted(k.id for k in kps))
check("没有知识点被漏掉", len(batches) * B >= len(kps))
empty = [kps[i:i + B] for i in range(0, 0, B)] or [[]]
check("空教案不会炸（退化成一批）", empty == [[]])

print()
print("=== 用例 3：_lesson_for_review 只带本批知识点，顶层字段照留 ===")
ids = {k.id for k in batches[0]}
text = rv._lesson_for_review(lesson, ids)
data = json.loads(text)
check("只带本批知识点", {k["id"] for k in data["knowledge"]} == ids,
      f"{len(data['knowledge'])} 个")
check("例题跟着知识点走", sum(len(k["examples"]) for k in data["knowledge"]) == len(ids))
check("顶层字段仍保留（目标是全局的）",
      bool(data.get("meta", {}).get("title")) and bool(data.get("objectives")))
check("文案体积明显小于整份", len(text) < len(rv._lesson_for_review(lesson)) * 0.6,
      f"{len(text)} vs {len(rv._lesson_for_review(lesson))}")
full = json.loads(rv._lesson_for_review(lesson))
check("不传 ids 时是全量", len(full["knowledge"]) == 11)

print()
print("=== 用例 4：_shots_for_review 的清单也只列本批 ===")
with tempfile.TemporaryDirectory() as td:
    shots = Path(td)
    for kp in lesson.knowledge:
        (shots / kp.board_shot).write_bytes(b"x")
        for ex in kp.examples:
            (shots / ex.shot).write_bytes(b"x")
    sub = rv._shots_for_review(lesson, shots, ids)
    # 行首 id 有两种：知识点用 kpN，例题用 exN。别把例题错当成"漏进来的知识点"。
    seen_kp = {ln.split("｜")[0].strip().lstrip("- ") for ln in sub.splitlines()
               if ln.startswith("- ") and ln.split("｜")[0].strip().lstrip("- ").startswith("kp")}
    seen_ex = {ln.split("｜")[0].strip().lstrip("- ") for ln in sub.splitlines()
               if ln.startswith("- ") and ln.split("｜")[0].strip().lstrip("- ").startswith("ex")}
    want_ex = {ex.id for k in lesson.knowledge if k.id in ids for ex in k.examples}
    check("只列本批的知识点行", seen_kp == ids, f"看到 {sorted(seen_kp)}")
    check("例题跟着本批知识点走，没有串批", seen_ex == want_ex, f"看到 {sorted(seen_ex)}")
    check("没有把别的批次混进来",
          not any(f"知识点{i}｜" in sub for i in range(1, 12) if f"kp{i}" not in ids))
    check("文件存在被如实标注", "文件存在" in sub)
    check("全量清单比批量长", len(rv._shots_for_review(lesson, shots)) > len(sub))
    # 空集合 = 不筛选，不是"筛成空"。全局核查传的就是 set()：
    # 当成空筛选会让审核员拿到一份 knowledge 为空、flow/homework 却照常
    # 引用着 kpN 的空壳教案，凭空造出一堆"引用断裂"的事实错误，
    # 实测直接把没问题的教案判成 fail。
    empty_set = rv._shots_for_review(lesson, shots, set())
    check("空集合按不筛选处理（给全量）", empty_set == rv._shots_for_review(lesson, shots))
    full_lesson = json.loads(rv._lesson_for_review(lesson, set()))
    check("空集合下知识点不被裁空", len(full_lesson["knowledge"]) == 11,
          f"剩 {len(full_lesson['knowledge'])} 个")
    empty_l = json.loads(rv._lesson_for_review(KnowledgePoint and Lesson(), set()))
    check("真的没有知识点时才是空", empty_l["knowledge"] == [])

print()
print("=== 用例 5：回炉按意见裁剪知识点 ===")
lesson5 = make_lesson(11)
iss = lambda t, p="", s="": rv.ReviewIssue(target=t, problem=p, suggestion=s)  # noqa: E731

check("按 kp id 定位", rv._issue_kp_ids([iss("kp3", "定义写错了")], lesson5) == {"kp3"})
check("按例题 id 反查所属知识点", rv._issue_kp_ids([iss("ex7", "答案对不上")], lesson5) == {"kp7"})
check("问题正文里提到 id 也能定位",
      rv._issue_kp_ids([iss("例题", "ex5 的题干和画面不一致")], lesson5) == {"kp5"})
check("多条意见取并集",
      rv._issue_kp_ids([iss("kp1"), iss("kp2"), iss("ex9")], lesson5) == {"kp1", "kp2", "kp9"})
check("定位不到就发全量（宁多勿漏）",
      rv._issue_kp_ids([iss("整体", "缺少课后作业")], lesson5) is None)
check("空的意见列表也不裁", rv._issue_kp_ids([], lesson5) is None)
# 这条最要命：ex1 是 ex10 的前缀，不加词边界就会把 kp10 一起拖进来
check("ex1 不会误命中 ex10", rv._issue_kp_ids([iss("", "ex1 的解析缺失")], lesson5) == {"kp1"})
check("目标串里的 id 也认", rv._issue_kp_ids([iss("ex11 有问题")], lesson5) == {"kp11"})

# 裁剪后单批报文必须明显变小，否则这次改造等于没做
full5 = rv._lesson_for_review(lesson5)
one5 = rv._lesson_for_review(lesson5, {"kp3"})
check("单批报文显著变小", len(one5) < len(full5) * 0.5, f"{len(one5)} vs {len(full5)}")

print()
print("=== 用例 6：裁剪输入后，没提到的知识点必须还在（安全底线）===")
from app.stages.compose import _to_lesson  # noqa: E402

full = make_lesson(11)
# 模拟模型只回了被裁到的那一个知识点
patch = {"knowledge": [{"id": "kp3", "summary": "改过的摘要"}]}
out = _to_lesson(patch, full.meta, full.knowledge, 45.0, fallback=full)
check("知识点一个没少", len(out.knowledge) == 11, f"剩 {len(out.knowledge)} 个")
check("被改的那个真的改了", next(k for k in out.knowledge if k.id == "kp3").summary == "改过的摘要")
check("没提到的知识点内容原样保留",
      next(k for k in out.knowledge if k.id == "kp7").definition == "定义 7")
check("例题没被洗掉", out.example_count() == 11, f"剩 {out.example_count()} 道")

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)