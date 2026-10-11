# -*- coding: utf-8 -*-
"""自检：判断题不许带编造的多步解析。

实测故障（ep119 第 5 轮）：视频里老师抛出一道抢答题
「斜率相等的两条直线，倾斜角一定相等，对不对呢？」，随口判了个「正确」。
模型却给它写了五步完整数学推导，其中还夹着「tanα 在 α≠90° 时单调」这种
并不严谨的说法。老师拿进课堂，学生照着记下来——错的不是教案而是板书。

判据只看题干本身：出现「对不对 / 是否正确 / 判断」这类说法就是判断题，
老师当场只给一个是非结论，不可能有推导过程。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint, Lesson, LessonMeta
from app.stages.compose import _trim_invented_solutions

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def make(stmt: str, steps: list[str], is_ex: bool = True) -> Lesson:
    ex = Example(id="ex1", title="例题", statement_plain=stmt, answer="正确",
                 solution=list(steps), is_exercise=is_ex)
    kp = KnowledgePoint(id="kp1", title="考点", examples=[ex])
    return Lesson(meta=LessonMeta(title="t"), knowledge=[kp])


FIVE_STEPS = [
    "由 kp5 已知：当 α ≠ 90° 时，倾斜角 α 与斜率 k = tanα 一一对应",
    "两条直线斜率相等，意味着 k₁ = k₂，即 tanα₁ = tanα₂",
    "在 α ∈ [0°, 180°) 内，tanα 在 α ≠ 90° 时单调，所以可推出 α₁ = α₂",
    "前提：两条直线的斜率都存在（即倾斜角都不取 90°）",
    "因此在「斜率存在」的前提下，命题正确",
]

# ---------------------------------------------------------------- 判断题：多步推导必须清掉
JUDGE = [
    "斜率相等的两条直线，倾斜角一定相等，对不对呢？",
    "下列说法是否正确：任意两条直线都有斜率。",
    "直线 l 的倾斜角为 120°，判断正误并说明理由。",
    "两直线垂直，对吗？",
]
for stmt in JUDGE:
    L = make(stmt, FIVE_STEPS)
    n = _trim_invented_solutions(L, L.knowledge)
    left = L.knowledge[0].examples[0].solution
    check(f"判断题的多步推导被清掉：{stmt[:18]}", n == 1 and left == [], f"剩 {len(left)} 步")

# ---------------------------------------------------------------- 非判断题：一步都不能动
REAL = [
    ("直线的倾斜角为 $\\alpha$，则 $\\sin\\alpha > 0$.", "判断式但没问对错"),
    ("已知直线 l 经过点 A(1, 2) 和点 B(3, -2)，求 l 的斜率。", "常规计算题"),
    ("把 $y=kx+b$ 中的 k 换成 2，写出解析式。", "基础填空"),
]
for stmt, why in REAL:
    L = make(stmt, FIVE_STEPS)
    n = _trim_invented_solutions(L, L.knowledge)
    left = L.knowledge[0].examples[0].solution
    check(f"非判断题不动解析：{why}", n == 0 and len(left) == 5, f"剩 {len(left)} 步")

# ---------------------------------------------------------------- 一句话说明不算推导
L = make("两条直线垂直，对吗？", ["因为斜率乘积为 -1。"])
n = _trim_invented_solutions(L, L.knowledge)
check("判断题的一句话依据保留", n == 0 and len(L.knowledge[0].examples[0].solution) == 1,
      f"返回 {n}")

# ---------------------------------------------------------------- 已降级的口述要点不参与
L = make("两条直线垂直，对吗？", FIVE_STEPS, is_ex=False)
n = _trim_invented_solutions(L, L.knowledge)
check("已降级的口述要点不受影响", n == 0 and len(L.knowledge[0].examples[0].solution) == 5)

# ---------------------------------------------------------------- 边界
L = make("", FIVE_STEPS)
check("没有题干就不处理", _trim_invented_solutions(L, L.knowledge) == 0)
check("空知识点列表安全", _trim_invented_solutions(make("对吗？", ["x"]), []) == 0)
L = make("对吗？", ["  ", "", "  "])
check("全是空步骤不算多步", _trim_invented_solutions(L, L.knowledge) == 0)

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails[:6]))
    raise SystemExit(1)
print("✅ 判断题不会被安上编造的多步解析")