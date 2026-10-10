"""标题清洗：把看图转写阶段长出来的元叙述括注剥掉。

看图那一遍很容易把"观察到什么"一起写进标题，于是教案里出现这种东西：

    例6（练习1（老师称其为'第一道简单的送分题'，画面上无题号标注，
        左上角红色手写'估算'二字为方法名））·比大小送分题

括号里那些是**看图笔记**，不是标题，直接印在老师的 Word 上很难看。
但要小心两件事：

1. 不能把有用的出处一起删掉。「（2022·新高考I卷）」是真信息，
   要从「题目标注为（2022·新高考I卷）」里把出处救出来，而不是整段删。
2. 不能删到括号不配对。原数据里就有 `））` 这种坏的。

只动标题，不动题干/答案/解析——那些是要一字不差的。
"""
from __future__ import annotations

import re

# 括注里出现这些词，说明写的是"我从画面上看到了什么"，不是标题本身
_META_WORDS = (
    "画面", "老师", "原样", "手写", "字幕", "视频", "听不清", "看不清",
    "无题号", "未见", "未显示", "显示为", "标注为", "左上角", "右上角",
    "红色", "黑色", "蓝色", "批注", "旁白", "口播", "标注",
)

# 括号里光是这样、没别的信息，纯属占位
_EMPTY_WORDS = {"题目", "例题", "练习", "例", "练习题", "例题讲解", "无标题", "题目截图"}

# 出处：2022·全国甲卷 / 2019 北京卷
_CITE_RE = re.compile(r"\d{4}\s*[·．\-]?\s*[^（）]{0,18}?卷")

# 最内层括号（里面不再嵌套）
_INNER_RE = re.compile(r"[（(]([^（）()]*)[)）]")

# 带一层嵌套的括号：外壳套着内壳，形如 （题目标注为（2022·全国甲卷））
_WRAP_RE = re.compile(r"[（(]([^（）()]*[（(][^（）()]*[）)][^（）()]*)[)）]")


def _cite_of(inner: str) -> str:
    """从一段元叙述里把出处捞出来，捞不到返回空串。"""
    m = _CITE_RE.search(inner)
    return m.group(0).strip() if m else ""


def _plan(inner: str) -> tuple[bool, str]:
    """这个括注该怎么处理。返回 (要不要动, 动成什么)。

    "要不要动"和"动成什么"必须分开——改动后的结果完全可能是空串
    （整段删掉），拿空串当"别动"的信号会漏改。
    """
    plain = re.sub(r"[（(][^（）()]*[）)]", "", inner)  # 剥掉嵌套部分，只看外壳文字
    norm = re.sub(r"\s+", "", plain).lower()
    is_meta = any(w in norm for w in _META_WORDS)
    is_placeholder = norm in _EMPTY_WORDS or not norm
    if not (is_meta or is_placeholder):
        return False, ""
    cite = _cite_of(inner)
    return True, (f"（{cite}）" if cite else "")


def _strip_once(title: str) -> tuple[str, bool]:
    """剥一层。返回 (剥完的串, 有没有发生变化)。"""
    changed = False

    def _sub(m: re.Match[str]) -> str:
        nonlocal changed
        act, out = _plan(m.group(1))
        if not act:
            return m.group(0)
        changed = True
        return out

    # 先拆外壳：「（题目标注为（2022·全国甲卷））」→「（2022·全国甲卷）」
    title = _WRAP_RE.sub(_sub, title)
    # 再剥内层：「（题目（画面…））」→「（题目）」→「」
    title = _INNER_RE.sub(_sub, title)
    return title, changed


def _balance(title: str) -> str:
    """把剥剩下的括号配平：多出的右括号丢掉，落单的左括号连内容一起去掉。"""
    out: list[str] = []
    depth = 0
    for ch in title:
        if ch in "（(":
            depth += 1
            out.append(ch)
        elif ch in "）)":
            if depth == 0:
                continue  # 落单的右括号，丢
            depth -= 1
            out.append(ch)
        else:
            out.append(ch)
    s = "".join(out)
    while depth:
        idx = max(s.rfind("（"), s.rfind("("))
        if idx < 0:
            break
        depth -= 1
        s = s[:idx]
    return s


def tidy_title(title: str) -> str:
    """把一个知识点/例题标题收拾干净。"""
    if not title:
        return title
    out = title
    for _ in range(6):  # 括注可能嵌套好几层，剥到不再变化为止
        out, changed = _strip_once(out)
        if not changed:
            break
    out = _balance(out)
    out = re.sub(r"[·、]{2,}", "·", out)
    out = re.sub(r"\s*[·、]\s*$", "", out)
    out = re.sub(r"^\s*[·、]\s*", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out or title.strip()  # 全剥光了也别弄成空串