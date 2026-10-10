"""OMML 转换自检：确认 LaTeX 能变成真正的 Word 公式，而不是图片。"""
from app.docx.omml import has_omml_support, latex_to_omml, split_math

print("OMML(XSL) available:", has_omml_support())
TESTS = [
    r"\frac{a+b}{c-d}",
    r"\sqrt{x^2+y^2}",
    r"\lim_{x \to 0} \frac{\sin x}{x} = 1",
    r"\int_{0}^{\pi} \sin x \, dx = 2",
    r"\begin{align} a^2+b^2 &= c^2 \\ 2+2 &= 4 \end{align}",
    r"f(x)=ax^2+bx+c,\quad \Delta=b^2-4ac",
    r"\text{已知} \triangle ABC \text{ 中有} a^2+b^2=c^2",
    r"\vec{a}\cdot\vec{b}=|\vec{a}||\vec{b}|\cos\theta",
    r"\sum_{i=1}^{n} i = \frac{n(n+1)}{2}",
]
ok = 0
for t in TESTS:
    omml = latex_to_omml(t)
    ok += bool(omml)
    head = t.replace("\n", " ")[:44]
    print(("OK  " if omml else "FAIL"), head, "->", (omml[:70] + "...") if omml else "None")
print(f"\nOMML 成功率: {ok}/{len(TESTS)}")

print("\n--- split_math ---")
for kind, body, _raw in split_math(r"已知 $f(x)=x^2$，则 $$x^2$$ 成立"):
    print(f"  {kind:6s} | {body[:40]}")