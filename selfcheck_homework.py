# -*- coding: utf-8 -*-
"""自检：作业必须来自视频，绝不允许编题。

背景：提示词里曾写着"homework 必须给 3~5 条、不许留空"，还教模型
"照着本课题型改编例题（同类型换数字），并注明教师自编"。于是模型为了交差
自己编了一整页视频里根本不存在的题。那不是备课，是伪造。
现在提示词已改口，另外加一道本地闸门 _ground_homework 兜住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Example, KnowledgePoint
from app.stages.compose import _ground_homework, _number_fingerprint

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# 视频里真实出现的两道题
kps = [KnowledgePoint(id="kp1", title="用两点坐标求斜率", examples=[
    Example(id="ex1", title="例1·两点求斜率", is_exercise=True,
            statement_latex="已知 $A(1,2)$、$B(3,6)$，求直线 $AB$ 的斜率 $k$。"),
    Example(id="ex2", title="抢答判断题1", is_exercise=True,
            statement_latex="直线的倾斜角为 $\\alpha$，则 $\\sin\\alpha > 0$。"),
])]
# 一条口述要点（不是真例题）即便写了数字，也不该成为作业出处
kps.append(KnowledgePoint(id="kp2", title="概念", examples=[
    Example(id="ex3", title="口述", is_exercise=False,
            statement_latex="老师举了 $A(7,9)$ 这个例子说明斜率。"),
]))

video_nums = _number_fingerprint("已知 $A(1,2)$、$B(3,6)$，求直线 $AB$ 的斜率 $k$。")

# ---------------------------------------------------------------- 1. 编造的题必须被拦下
fab = [
    "已知 A(1,3) 求斜率。（教师自编，参照本课例题 1）",   # 数字对不上 + 自编标注
    "若直线 (a-1)x + y - 2 = 0 与 2x + ay - 1 = 0 互相垂直，求 a。",  # 全新题
    "已知直线 l 经过点 A(7, 9) 和点 B(2, 8)，求斜率。",             # 换数字
]
check("编造的作业全部被拦下", _ground_homework(fab, kps) == [],
      f"剩 {len(_ground_homework(fab, kps))} 条")

# ---------------------------------------------------------------- 2. 忠实于原题的保留
real = [
    "已知点 A(1,2)、B(3,6)，求直线 AB 的斜率 k。",            # 原题忠实转述
    "1. 已知 A(1,2) 与 B(3,6)，求斜率。",                       # 带序号
]
kept = _ground_homework(real, kps)
check("忠实于视频原题的作业保留", len(kept) == 2, f"剩 {len(kept)} 条")

# ---------------------------------------------------------------- 3. 混合场景
mixed = real + fab
kept = _ground_homework(mixed, kps)
check("真假混合时只留真的", len(kept) == 2 and all("自编" not in k for k in kept))

# ---------------------------------------------------------------- 4. 边界
check("空输入返回空", _ground_homework([], kps) == [])
check("没有可用例题时返回空（宁可空着也不编）",
      _ground_homework(real, []) == [])
check("口述要点不能作为作业出处",
      _ground_homework(["已知 $A(7,9)$ 与 $B(2,8)$，求斜率。"], kps) == [])
check("不带数字的短句放行（谈不上照着改编）",
      _ground_homework(["复习本节课笔记"], kps) == ["复习本节课笔记"])

# ---------------------------------------------------------------- 5. 数字指纹本身
check("数字指纹忽略小数尾零",
      _number_fingerprint("k = 2.50") == _number_fingerprint("k = 2.5"))
check("数字指纹能区分不同数值",
      _number_fingerprint("A(1,2)") != _number_fingerprint("A(1,3)"))

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails))
    raise SystemExit(1)
print("✅ 作业只认视频里的题，编造的一律拦下")