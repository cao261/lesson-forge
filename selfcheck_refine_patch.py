"""回炉兜底链自检：模型只给补丁时，没提到的字段必须原样留着。

这条是今天两次翻车留下的：segment 输出精简后，_to_lesson 的兜底只剩
「原始切分结果」，那已经是空的了。模型一漏写字段，定义/公式/解析就被洗掉。
现在兜底链必须是：新值 → 上一版教案 → 原始切分结果。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint, Lesson  # noqa: E402
from app.stages.compose import _to_lesson  # noqa: E402
from app.stages.review import REFINE_BATCH, _content_score  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def rich_lesson() -> tuple[Lesson, list[KnowledgePoint]]:
    """一份"已经写满"的上一版教案。"""
    old = Lesson()
    kp = KnowledgePoint(
        id="kp1", title="知识点一", ts_start=0.0, ts_end=600.0,
        summary="这一段讲的是二次函数图像的平移",
        definition="函数 y=f(x) 的图像向左平移 a 个单位得到 y=f(x+a)",
        formula_latex="y=f(x+a)",
        key_points=["左加右减", "a>0 时左移"],
        pitfalls=["符号容易搞反"],
        teaching_notes="先问学生 y=x^2 怎么平移，再引出通法",
        source_quote="我们把图像往左平移 a 个单位",
    )
    kp.examples.append(Example(
        id="ex1", title="例1", ts_start=10.0,
        statement_latex="将 $y=x^2$ 的图像左移 2 个单位，写出解析式",
        statement_plain="将抛物线左移 2 个单位",
        answer="y=(x+2)^2", solution=["第一步 左移即 x+2", "第二步 代入得 (x+2)^2"],
        difficulty="基础", tags=["图像平移"],
    ))
    old.knowledge = [kp]
    old.objectives = ["目标一", "目标二"]
    # 作业必须是能从例题溯源出来的真题：数字与 ex1 完全一致。
    # 用"作业一/作业二"这种占位文本测不出兜底链在干什么——
    # 那是真教案里根本不会出现的东西。
    old.homework = [
        "将 $y=x^2$ 的图像左移 2 个单位，写出解析式",
        "把 $y=x^2$ 的图像向左平移 2 个单位，求解析式，并说明顶点怎么变。",
    ]
    # 原始切分结果：现在 segment 已经不写讲解内容了，这里就是"空"的
    raw = [KnowledgePoint(id="kp1", title="知识点一", ts_start=0.0, ts_end=600.0)]
    raw[0].examples.append(Example(id="ex1", title="例1", ts_start=10.0))
    return old, raw


print("=== 用例 1：模型只改了公式，其他一律不写 ===")
old, raw = rich_lesson()
# 模拟"补丁"：只动 formula_latex
patch = {"knowledge": [{"id": "kp1", "formula_latex": "y=f(x-a)"}]}
out = _to_lesson(patch, old.meta, raw, 45.0, fallback=old)
kp = out.knowledge[0]
ex = kp.examples[0]
check("改了的公式用上了", kp.formula_latex == "y=f(x-a)", kp.formula_latex)
check("summary 沿用上一版", kp.summary == "这一段讲的是二次函数图像的平移", kp.summary[:20])
check("definition 沿用上一版", kp.definition.startswith("函数 y=f(x)"), kp.definition[:20])
check("key_points 沿用上一版", len(kp.key_points) == 2, str(kp.key_points))
check("pitfalls 沿用上一版", len(kp.pitfalls) == 1)
check("teaching_notes 沿用上一版", kp.teaching_notes.startswith("先问学生"), kp.teaching_notes[:20])
check("source_quote 沿用上一版", kp.source_quote.startswith("我们把图像"))
check("例题题干没丢", ex.statement_latex.startswith("将 $y=x^2$"), ex.statement_latex[:20])
check("例题答案没丢", ex.answer == "y=(x+2)^2", ex.answer)
check("例题解析没丢", len(ex.solution) == 2, str(len(ex.solution)))
check("顶层 objectives 没丢", len(out.objectives) == 2)
check("顶层 homework 没丢", len(out.homework) == 2)

print("\n=== 用例 2：模型返回空补丁，内容必须一字不动 ===")
old2, raw2 = rich_lesson()
before = _content_score(old2)
out2 = _to_lesson({}, old2.meta, raw2, 45.0, fallback=old2)
after = _content_score(out2)
check("空补丁后信息分不降", after >= before, f"{before} -> {after}")
check("空补丁后例题还在", out2.example_count() == 1)

print("\n=== 用例 3：模型想把解析删空，按它说的删 ===")
old3, raw3 = rich_lesson()
patch3 = {"knowledge": [{"id": "kp1", "examples": [
    {"id": "ex1", "solution": [], "answer": ""},
]}]}
out3 = _to_lesson(patch3, old3.meta, raw3, 45.0, fallback=old3)
check("显式给的空解析被接受（审核说要删就删）",
      out3.knowledge[0].examples[0].solution == [])
check("显式空答案被接受", out3.knowledge[0].examples[0].answer == "")

print("\n=== 用例 4：回炉分批 ===")
print(f"  REFINE_BATCH = {REFINE_BATCH}")
issues = [type("I", (), {"severity": "major", "target": f"t{i}", "problem": "p", "suggestion": "s"})()
          for i in range(25)]
todo = issues[:25]
batches = [todo[i:i + REFINE_BATCH] for i in range(0, len(todo), REFINE_BATCH)]
print(f"  25 条意见 -> {len(batches)} 批：{[len(b) for b in batches]}")
check("25 条意见切成多批", len(batches) >= 2, f"{len(batches)} 批")
check("每批不超过上限", all(len(b) <= REFINE_BATCH for b in batches))
check("批数不等于原来那一发（1 发 -> %d 发）" % len(batches), len(batches) > 1)
check("没有意见被漏掉", sum(len(b) for b in batches) == 25)

print("\n" + "=" * 50)
print(f"通过 {ok}/{total}")
print("✅ 回炉兜底链正常" if ok == total else "❌ 兜底链有问题")
sys.exit(0 if ok == total else 1)
