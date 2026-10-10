"""LaTeX → Word 公式（OMML）。

Word 的公式不是图片，是一个叫 OMML 的 XML 树。转换链路：
    LaTeX --latex2mathml--> MathML --MML2OMML.XSL--> OMML
MML2OMML.XSL 随 Microsoft Office 一起安装，本机探测不到时回退到高清位图。
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.config import SETTINGS

MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# 常见的、需要先剥掉的 LaTeX 装饰
_STRIP_ENVS = ("equation", "equation*", "align", "align*", "gather", "gather*", "multline", "multline*", "split")


class LatexError(ValueError):
    pass


def split_lines(latex: str) -> list[str]:
    """把多行公式（align / cases / 用 \\\\ 分行）拆成一行一个独立公式。

    Word 里没有"对齐符 &"的概念，拆成多段居中公式比硬塞一个 eqArr 更稳、更好看。
    """
    s = latex.strip()
    s = re.sub(r"^\\\[|\\\]$", "", s).strip()
    s = re.sub(r"^\\\(|\\\)$", "", s).strip()
    for env in _STRIP_ENVS:
        s = re.sub(rf"\\begin\{{{env}\*?\}}(.*?)\\end\{{{env}\*?\}}", r"\1", s, flags=re.S)
    # cases / aligned 之类保留了缩进含义的环境，也按行拆
    for env in ("cases", "aligned", "gathered", "array", "matrix", "pmatrix", "bmatrix"):
        s = re.sub(rf"\\begin\{{{env}\}}(.*?)\\end\{{{env}\}}", r"\1", s, flags=re.S)
    parts = [ln.strip() for ln in re.split(r"\\\\", s) if ln.strip()]
    # 去掉 align 的 & 对齐符
    return [re.sub(r"(?<!\\)&", "", p).strip() for p in parts if p.strip("& \t")]


def _strip_wrapper(latex: str) -> str:
    """取单行公式本体：拆环境、去 &、取第一行。"""
    lines = split_lines(latex)
    return lines[0] if lines else ""


@lru_cache(maxsize=1)
def _transform():
    from lxml import etree

    xsl_path = SETTINGS.mml2omml_xsl
    if not xsl_path or not Path(xsl_path).exists():
        return None
    try:
        return etree.XSLT(etree.parse(xsl_path))
    except Exception:  # noqa: BLE001
        return None


def latex_to_omml(latex: str) -> str | None:
    """返回 OMML XML 字符串；任何一环失败都返回 None，由调用方走位图兜底。"""
    core = _strip_wrapper(latex)
    if not core:
        return None
    try:
        from latex2mathml.converter import convert
    except ImportError:
        return None

    try:
        mathml = convert(core)
    except Exception:  # noqa: BLE001
        return None
    if not mathml or "<math" not in mathml:
        return None

    transform = _transform()
    if transform is None:
        return None

    from lxml import etree

    try:
        result = transform(etree.fromstring(mathml.encode("utf-8")))
        omml = str(result)
    except Exception:  # noqa: BLE001
        return None

    # 去掉 XML 声明：python-docx 的 parse_xml 不接受带 encoding 的声明
    omml = re.sub(r"^\s*<\?xml[^>]*\?>\s*", "", omml).strip()
    if "oMath" not in omml:
        return None
    # 补上命名空间，否则 python-docx 插入时会报错
    if f'xmlns:m="{MATH_NS}"' not in omml:
        omml = omml.replace("<m:oMath", f'<m:oMath xmlns:m="{MATH_NS}" xmlns:w="{W_NS}"', 1)
    return omml


def latex_to_image(latex: str, out_png: Path, dpi: int = 300, fontsize: int = 20) -> Path | None:
    """兜底方案：用 matplotlib 的 mathtext 渲染成高清 PNG。"""
    core = _strip_wrapper(latex)
    if not core:
        return None
    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return None

    try:
        fig = plt.figure(figsize=(0.02, 0.02), dpi=dpi)
        fig.patch.set_alpha(0.0)
        fig.text(0, 0, f"${core}$", fontsize=fontsize, color="#1a1a1a")
        fig.savefig(out_png, dpi=dpi, bbox_inches="tight", pad_inches=0.06, transparent=True)
        plt.close(fig)
    except Exception:  # noqa: BLE001
        plt.close(fig) if "fig" in dir() else None
        return None
    return out_png if out_png.exists() else None


def has_omml_support() -> bool:
    return _transform() is not None


# ------------------------------------------------------------------ 公式切分
_MATH_PATTERNS = [
    re.compile(r"\$\$(.+?)\$\$", re.S),
    re.compile(r"\\\[(.+?)\\\]", re.S),
    re.compile(r"(?<!\$)\$([^$\n]+?)\$(?!\$)", re.S),
    re.compile(r"\\\((.+?)\\\)", re.S),
]

DELIM_KIND = {"$$": "block", "\\[": "block", "$": "inline", "\\(": "inline"}


def split_math(text: str) -> list[tuple[str, str, str]]:
    """把一段混排文本切成 [(类型, 内容, 原文)]，类型 ∈ {text, inline, block}。

    例：'已知 $f(x)=x^2$，求 $f(2)$' → [('text','已知 '), ('inline','f(x)=x^2'), ('text','，求 '), ...]
    """
    out: list[tuple[str, str, str]] = []
    i = 0
    buf: list[str] = []
    n = len(text)
    while i < n:
        matched = False
        for pat in _MATH_PATTERNS:
            m = pat.match(text, i)
            if not m:
                continue
            opener = m.group(0)
            kind = "block" if opener.startswith("$$") or opener.startswith("\\[") else "inline"
            body = m.group(1).strip()
            if body:
                if buf:
                    out.append(("text", "".join(buf), "".join(buf)))
                    buf = []
                out.append((kind, body, opener + m.group(1) + opener[-1]))
                i = m.end()
                matched = True
                break
        if matched:
            continue
        buf.append(text[i])
        i += 1
    if buf:
        joined = "".join(buf)
        if joined:
            out.append(("text", joined, joined))
    return out


def has_math(text: str) -> bool:
    return any(kind != "text" for kind, _, _ in split_math(text))