"""导出前的文本清洗：保证 Word 里出现的是人能读的内容。

模型偶尔会把思考过程、代码围栏、JSON 片段带进正文，JSON 解析层已经拦掉了大部分，
但字段值本身仍可能脏。这一层在**排版之前**统一扫一遍，属于最后一道防线。

原则：宁可少删也不误删正文。拿不准的一律保留，交给审核环节去报问题。
"""
from __future__ import annotations

import re

# 模型的思考过程
_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_OPEN = re.compile(r"<think>.*", re.S | re.I)
# 代码围栏
_FENCE_BLOCK = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n.*?```", re.S)
_FENCE_LINE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$", re.M)
# 裸露的 JSON 键值行，例如 {"problem": "xxx"
_JSON_LINE = re.compile(r'^\s*[\{\}]\s*"?[\w一-鿿]+"?\s*[:：].*$')
# 客套话。只认"元话语"开头，且必须带标点——
# 早先的版本把「这是」「根据」也当客套话，结果把"这是重点"开头的正常句子砍掉了头。
_LEADIN = re.compile(r"^\s*(?:好的|好|OK|ok|输出|Output|output|Answer|answer)\s*[:：,，]\s*")
# Markdown 强调符号（正文里的 * 和 _ 不是公式的话就没必要）
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _balance_dollars(text: str) -> str:
    """$ 定界符不成对时处理掉落单的那个。

    选择**删掉**而不是补一个闭合符：模型写成 `$x^2-1<0 的解集` 时，
    补一个收尾会把整句中文吞进公式里渲染，反而更糟；删掉之后正文仍是可读的纯文本。
    这种情况规则体检本来就会报出来，提醒老师复核。
    """
    dollars = re.findall(r"(?<!\\)\$", text)
    if len(dollars) % 2 == 0:
        return text
    idx = text.rfind("$")
    if idx < 0:
        return text
    # 确认这个 $ 没被反斜杠转义
    backslashes = 0
    k = idx - 1
    while k >= 0 and text[k] == "\\":
        backslashes += 1
        k -= 1
    if backslashes % 2 == 1:
        return text
    return text[:idx] + text[idx + 1:]


def clean(text: str) -> str:
    """把一段模型产出的文本洗成可以直接排版的正文。"""
    if not text:
        return ""

    out = _THINK.sub("", text)
    out = _THINK_OPEN.sub("", out)
    out = _FENCE_BLOCK.sub("", out)
    out = _FENCE_LINE.sub("", out)

    lines: list[str] = []
    for line in out.splitlines():
        if _JSON_LINE.match(line):
            continue
        lines.append(line)
    out = "\n".join(lines)

    out = _CTRL.sub("", out)
    out = out.replace("\r\n", "\n").replace("\r", "\n")
    # 三行以上空行压成两行
    out = re.sub(r"\n{3,}", "\n\n", out)
    # 行尾空格
    out = "\n".join(ln.rstrip() for ln in out.split("\n"))

    out = out.strip()
    out = _LEADIN.sub("", out)
    out = _MD_BOLD.sub(r"\1", out)

    out = _balance_dollars(out)
    return out.strip()


def clean_block(text: str) -> str:
    """清洗一个多行块（解析步骤、讲解备注）。"""
    out = clean(text)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def looks_dirty(text: str) -> list[str]:
    """体检用：返回这段文本里还残留的脏东西类型名，干净则返回空列表。"""
    bad: list[str] = []
    if re.search(r"<think>|</think>", text, re.I):
        bad.append("think")
    if "```" in text:
        bad.append("fence")
    if re.search(r'\{\s*"\w+"\s*:', text):
        bad.append("json")
    if re.search(r"(?<!\\)\$", text) and len(re.findall(r"(?<!\\)\$", text)) % 2 == 1:
        bad.append("dollar")
    if re.search(r"^\s*[\{\}]\s*\"?[\w一-鿿]+\"?\s*[:：].*$", text, re.M):
        bad.append("json_line")
    return bad