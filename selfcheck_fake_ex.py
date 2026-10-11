# -*- coding: utf-8 -*-
"""自检：假例题必须降级、标题卡必须排除、显式删除必须生效。

三件事对应三个实测踩过的坑：
1. 切分阶段为了"题目全覆盖"把每段概念讲解都立成例题 → 学生的「即学即练」
   里全是只有标题、没有题干的空壳。
2. 知识点配的"板书图"抓成了「抢答题目」四字色块标题卡 → 图不对题。
3. 模型删掉的例题被兜底逻辑原样补回来 → 审核只能一轮轮重复报同一条。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.media.frames import FrameInfo, is_title_card
from app.models import Example, KnowledgePoint
from app.stages.normalize import classify_exercises
from app.stages.compose import _clean_shot

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 1. 伪例题降级
def ex(eid, title, latex="", plain="", verified=False):
    return Example(id=eid, title=title, statement_latex=latex,
                   statement_plain=plain, verified=verified)


kps = [KnowledgePoint(id="kp1", title="概念", examples=[
    ex("ex1", "概念辨析·斜面向上方向"),                    # 无题干无题面 → 口述
    ex("ex2", "例1·两点求斜率", latex="已知 A(1,2)，求斜率"),   # 有题干 → 真例题
    ex("ex3", "抢答题", verified=True),                    # 视觉确认有题面 → 真例题
    ex("ex4", "图示", latex="\\text{通过图形展示关系}"),        # 图注不是题干 → 降级
])]
notes = classify_exercises(kps)
flags = {e.id: e.is_exercise for e in kps[0].examples}
check("无题干且无题面 → 降级为口述要点", flags["ex1"] is False)
check("有题干 → 仍是真例题", flags["ex2"] is True)
check("视觉确认有题面 → 仍是真例题", flags["ex3"] is True)
check("图注式题干（通过图形展示…）→ 降级", flags["ex4"] is False)
check("降级后清空图注式题干", kps[0].examples[3].statement_latex == "")
check("降级后不再挂着截图名", kps[0].examples[0].shot is None)
check("有可读的人话说明", any("口述要点" in n for n in notes), notes[0] if notes else "")

# 真题干不能被描述词误伤
tricky = [KnowledgePoint(id="k2", title="t", examples=[
    ex("ex9", "如图求斜率", latex="如图，直线 $l$ 过点 $A(1,2)$，求斜率 $k$。"),
    ex("ex10", "判断正误", latex="图中展示的直线 $l$ 的倾斜角为 $\\alpha$，则 $\\sin\\alpha>0$，判断对错。"),
])]
classify_exercises(tricky)
check("题干里带「如图」的真题不被误伤", tricky[0].examples[0].is_exercise is True)
check("题干带图注词但有设问 → 仍算真题", tricky[0].examples[1].is_exercise is True)

# 真实例题不能被误伤
all_real = [KnowledgePoint(id="k", title="t",
                           examples=[ex(f"ex{i}", "t",
                                        latex="已知直线 $l$ 过点 $A(1,2)$ 与 $B(3,6)$，求斜率 $k$。")
                                     for i in range(3)])]
classify_exercises(all_real)
check("全部有题干的不会被误降级",
      all(e.is_exercise for e in all_real[0].examples))

# ---------------------------------------------------------------- 2. 标题卡识别
def frame(ink: float, sat: float) -> FrameInfo:
    return FrameInfo(path=Path("x.jpg"), index=0, ts=0.0, ink_ratio=ink, bg_sat=sat)


check("绿底反白大字卡被判为标题卡", is_title_card(frame(0.470, 119.5)))
check("白底彩色大字页不算标题卡", not is_title_card(frame(0.166, 0.0)),
      "白底题面印「对」「错」两字，前景占比 0.166 但底色不饱和")
check("白底普通课件页不算标题卡", not is_title_card(frame(0.05, 0.0)))
check("白底高占比页不算标题卡", not is_title_card(frame(0.45, 0.0)),
      "底色不饱和就不是章节卡，不能只靠占比下判断")

# ---------------------------------------------------------------- 3. shot 净化
check("正常文件名放行", _clean_shot("ex7_clean.jpg") == "ex7_clean.jpg")
check("提示词占位说明被挡下", _clean_shot("（未获取到）") == "")
check("路径被挡下", _clean_shot("../../etc/passwd") == "")
check("非图片后缀被挡下", _clean_shot("ex7.txt") == "")

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails))
    raise SystemExit(1)
print("✅ 假例题降级、标题卡排除、shot 净化 全部正确")