"""归一化比较自检：只有"真的读错了"才判分歧，写法差异一律放过。"""
from app.stages.vision import _diff_hint, _normalize_for_compare as N

# (版本A, 版本B, 说明, 期望是否判为分歧)
CASES = [
    ("$x < m$", "$x < -m$", "少一个负号", True),
    ("$a^2+b^2=c^2$", "$a^2-b^2=c^2$", "加减号不同", True),
    ("$0<a<1$", "$0 \\le a < 1$", "不等号不同", True),
    ("$\\Delta=b^2-4ac$", "$\\Delta=b^2+4ac$", "判别式符号不同", True),
    ("$x^2-5x+6=0$", "$x^2-5x+7=0$", "常数不同", True),
    ("答案选 C", "答案选B", "选项不同", True),
    ("", "多出内容", "一边为空", True),
    # ---- 以下都应该判为"一致"，不该白白触发仲裁 ----
    ("$\\frac{1}{2}$", "$\\dfrac{1}{2}$", "frac / dfrac", False),
    ("$\\tfrac{1}{2}$", "$\\frac{1}{2}$", "tfrac / frac", False),
    ("$a \\cdot b$", "$a * b$", "cdot / 乘号", False),
    ("$x \\le 3$", "$x <= 3$", "le / <=", False),
    ("$x^2$", "$x²$", "LaTeX 上标 / Unicode 上标", False),
    ("$x \\to 0$", "$x -> 0$", "to / 箭头", False),
    ("已知 $x=1$，求 $y$", "已知 $x=1$;求 $y$", "标点差异", False),
    ("$\\left(x+1\\right)$", "$(x+1)$", "left/right 修饰", False),
]

print("=" * 72)
print("跨帧转写比对：什么时候该仲裁，什么时候该放过")
print("=" * 72)
ok = True
for a, b, label, want in CASES:
    diff = N(a) != N(b)
    good = diff == want
    ok &= good
    verdict = "分歧→仲裁" if diff else "一致→放过"
    print(f"[{'OK  ' if good else 'FAIL'}] {label:22s} {verdict}")
    if diff and good:
        print(f"          差异定位：{_diff_hint(a, b)}")

print()
if ok:
    print("✅ 逻辑正确：真读错才仲裁，写法差异不误报（省下大量无谓的模型调用）")
else:
    print("❌ 仍存在误判，见上面 FAIL 行")

print()
print("=" * 72)
print("典型场景：这道题两次转写不一致时会怎么提示老师")
print("=" * 72)
a = '【例2】若 "-2 < x < m" 是 "x^2 - x - 6 < 0" 的必要不充分条件，则实数 m 的取值范围是____。'
b = '【例2】若 "-2 < x < -m" 是 "x^2 - x - 6 < 0" 的必要不充分条件，则实数 m 的取值范围是____。'
print(f"  版本一: {a}")
print(f"  版本二: {b}")
print()
print(f"  → 交给模型仲裁（同时给两张图逐字核对）")
print(f"  → 若仲裁不了，教案里写：⚠ 两帧转写不一致且无法仲裁，请人工核对：{_diff_hint(a, b)}")
print(f"  → 并且该题标记为 ⚠ 未经确认，审核环节会把它列进问题清单")