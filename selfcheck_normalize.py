"""结构体检自检：用因式分解那节课的真实例题当用例。

那节课审核报的 blocker 是「例3 与例2 第(2)题完全重复」，还捎带两条
「例题时间戳互相重叠」。这类问题回炉改不动（回炉只能改文字，不能删题），
于是两轮下来一模一样——这就是"跑很久还抽卡"的来源。

用例全部取自 workspace/b06c084c21/lesson.json 的真实题干。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint  # noqa: E402
from app.stages import normalize as nz  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def ex(eid, s, e, stmt, plain=""):
    return Example(id=eid, title=eid, ts_start=s, ts_end=e,
                   statement_latex=stmt, statement_plain=plain)


print("=== 用例 1：ex4/ex5 是同一道三小题，自动删掉一道 ===")
# 真实数据：只是空格写法不同，相似度 0.95，重叠 44%
kp = KnowledgePoint(id="kp4", title="十字相乘", ts_start=700.0, ts_end=1000.0)
kp.examples = [
    ex("ex4", 739, 859, r"$x^2 - 3x - 4 =$  $\quad$  $2x^2 + 3x - 2 =$  $\quad$  $6x^2 - x - 1 =$"),
    ex("ex5", 806, 926, r"$x^2 - 3x - 4 = \qquad 2x^2 + 3x - 2 = \qquad 6x^2 - x - 1 ="),
]
notes = nz.normalize_kps([kp], duration=2000.0)
check("删掉了一道重复题", len(kp.examples) == 1, f"剩 {len(kp.examples)} 道：{[e.id for e in kp.examples]}")
check("保留的是先出现的那道", kp.examples and kp.examples[0].id == "ex4")
check("日志说明了删了什么", any("重复例题" in n for n in notes), str(notes[:1]))

print()
print("=== 用例 2：ex2/ex3 是整题和它的小题——只报不删 ===")
# 这类删不得：老师完全可能先讲整题、再单独讲其中一问
kp2 = KnowledgePoint(id="kp3", title="根式化简", ts_start=290.0, ts_end=620.0)
kp2.examples = [
    ex("ex2", 367, 487, r"(1) $\sqrt{5-2\sqrt{6}} =$   (2) $\sqrt{4-\sqrt{15}} =$"),
    ex("ex3", 481, 601, r"$\sqrt{4-\sqrt{15}} =$"),
]
nz.normalize_kps([kp2], duration=2000.0)
check("两道都保留（不替用户删）", len(kp2.examples) == 2, f"剩 {len(kp2.examples)} 道")
sus = nz.suspicious_pairs([kp2])
check("但报出了包含关系", len(sus) == 1, str(sus))
check("报的内容点明了 ex2/ex3", "ex2" in sus[0] and "ex3" in sus[0] if sus else False)

print()
print("=== 用例 3：真·不同的题不能被误删 ===")
kp3 = KnowledgePoint(id="kp9", title="求值", ts_start=1320.0, ts_end=1450.0)
kp3.examples = [
    ex("ex9", 1323, 1443,
       r"若 $x^2+xy-2y^2=0$，则 $\dfrac{x^2+3xy+y^2}{x^2+y^2}=\underline{\qquad\qquad}.$"),
    ex("ex11", 1861, 1981, r"因式分解：$3x^3 - 8x^2 + 3x + 2$"),
]
nz.normalize_kps([kp3], duration=2000.0)
check("两道都留着", len(kp3.examples) == 2)
check("没报包含关系", nz.suspicious_pairs([kp3]) == [])

# 题干一样但时间完全不挨着 —— 老师可能在课尾把同一道题又讲了一遍
kp4 = KnowledgePoint(id="kpX", title="重复讲解", ts_start=0.0, ts_end=2000.0)
kp4.examples = [
    ex("a", 100, 200, r"$x^2+2xy-3y^2-5x-7y+6 =$"),
    ex("b", 1500, 1600, r"$x^2+2xy-3y^2-5x-7y+6 =$"),
]
nz.normalize_kps([kp4], duration=2000.0)
check("离得远的同题干不删（可能是复习）", len(kp4.examples) == 2, f"剩 {len(kp4.examples)} 道")

print()
print("=== 用例 4：时间越界要裁 ===")
kp5 = KnowledgePoint(id="kpZ", title="越界", ts_start=300.0, ts_end=400.0)
kp5.examples = [ex("e1", 250, 450, r"$x=1$")]
notes5 = nz.normalize_kps([kp5], duration=1000.0)
e1 = kp5.examples[0]
check("例题时间被裁进知识点范围内",
      e1.ts_start >= kp5.ts_start and e1.ts_end <= kp5.ts_end,
      f"{e1.ts_start:.0f}–{e1.ts_end:.0f}s（知识点 {kp5.ts_start:.0f}–{kp5.ts_end:.0f}s）")
check("裁剪有日志", any("越出知识点范围" in n for n in notes5), str(notes5[:1]))

# 视频总长也要兜住
kp6 = KnowledgePoint(id="kpY", title="超出视频", ts_start=0.0, ts_end=1200.0)
kp6.examples = [ex("e1", 100, 1100, r"$y=2$")]
nz.normalize_kps([kp6], duration=600.0)
check("不超过视频总长", kp6.examples[0].ts_end <= 600.0,
      f"ts_end={kp6.examples[0].ts_end:.0f}")
check("裁完仍然是正长度", kp6.examples[0].ts_end > kp6.examples[0].ts_start)

print()
print("=== 用例 5：空输入不炸 ===")
check("没有知识点", nz.normalize_kps([], 100.0) == [])
check("没有例题", len(nz.normalize_kps([KnowledgePoint(id="k")], 100.0)) == 0)
check("suspicious 对空列表", nz.suspicious_pairs([]) == [])
empty_kp = KnowledgePoint(id="k", ts_start=0.0, ts_end=10.0)
empty_kp.examples = [Example(id="e"), Example(id="f")]
nz.normalize_kps([empty_kp], 100.0)
check("题干都空时不去重", len(empty_kp.examples) == 2, f"剩 {len(empty_kp.examples)} 道")
check("题干空也不报包含", nz.suspicious_pairs([empty_kp]) == [])

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)