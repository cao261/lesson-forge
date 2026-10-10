"""标题清洗自检：用真实数据里出现过的那些烂标题当用例。

这些标题来自"图像+比大小"那节课的看图转写结果，原样进了 Word。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.textnorm import tidy_title  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def eq(name: str, raw: str, want: str) -> None:
    got = tidy_title(raw)
    check(name, got == want, f"得到「{got}」" + ("" if got == want else f"，应为「{want}」"))


print("=== 用例 1：真实数据里的四个难缠标题 ===")
eq("剥离观察笔记，保留出处",
   "例6（练习1（老师称其为'第一道简单的送分题'，画面上无题号标注，"
   "左上角红色手写'估算'二字为方法名））·比大小送分题",
   "例6（练习1）·比大小送分题")

eq("嵌套里的画面描述剥掉",
   "例1（题目（画面上的原样标注为'浙江：'））·指对数综合求a",
   "例1·指对数综合求a")

eq("从叙述里把出处救出来",
   "例11（练习（题目标注为（2022·全国甲卷）））·比大小",
   "例11（2022·全国甲卷）·比大小")

eq("出处直接写着的保留",
   "例12（例题（2022·新高考I卷））·比大小：结构相似凑结构",
   "例12（2022·新高考I卷）·比大小：结构相似凑结构")

print()
print("=== 用例 2：干净的标题不能被误伤 ===")
for raw in ["指对数公式与化同底思想",
            "函数图像特征与四步策略",
            "比大小：估算与中间量",
            "形式相近比大小：构造函数",
            "（2022·全国甲卷）比大小",
            "二次函数图像平移"]:
    check(f"原样保留：{raw}", tidy_title(raw) == raw, f"得到「{tidy_title(raw)}」")

eq("正常的括注保留", "三角函数（第二类）图像", "三角函数（第二类）图像")
eq("只有编号的保留", "例2（例1）·函数图象选择题", "例2（例1）·函数图象选择题")

print()
print("=== 用例 3：括号必须配平 ===")
bad = [
    "例3（题目（画面无编号））",
    "例4（例2））·三角分式",
    "例5（（（画面）））",
    "）例6（练习）",
]
for raw in bad:
    got = tidy_title(raw)
    bal = got.count("（") == got.count("）") and got.count("(") == got.count(")")
    check(f"配平：{raw}", bal, f"得到「{got}」")

print()
print("=== 用例 4：不能洗成空串 ===")
for raw in ["（画面上的原样标注）", "（题目）", "（（画面无标注））"]:
    got = tidy_title(raw)
    check(f"不返回空：{raw}", bool(got.strip()), f"得到「{got}」")
check("空输入安全", tidy_title("") == "")
check("None 安全", tidy_title(None) is None)

print()
print("=== 用例 5：剥到稳定（不会来回震荡）===")
for raw in ["例1（题目（画面上的原样标注为'浙江：'））·指对数综合求a",
            "例6（练习1（老师称其为'送分题'，画面上无题号标注））·比大小送分题"]:
    once = tidy_title(raw)
    twice = tidy_title(once)
    check(f"幂等：{raw[:12]}…", once == twice, f"「{once}」 vs 「{twice}」")

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)