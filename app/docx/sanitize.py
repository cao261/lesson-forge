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

# ------------------------------------------------------------------ 元信息（视频分析口吻）
# 教案的读者是**看过视频的老师本人**，不是要了解视频制作过程的人。
# 提示词已经禁止模型写这些，但提示词只是"请求"——这里再加一道确定性防线。
#
# 判据刻意保守：整句命中才丢。宁可漏掉一句元信息，也不要把正文误伤。
# 全部用非捕获组，避免 findall 返回元组。
_VIDEO_TS = re.compile(r"\d{1,2}:\d{2}(?::\d{2})?")          # 02:45 / 00:01:25
_META = re.compile(
    r"(?:视频位置|视频\s*\d|视频第|视频里|视频中|老师原话|老师说"
    r"|老师讲的?是|口述|未获取到|截图未获取|截图文件不存在|文件不存在"
    r"|题干未转写|转写失败|板书(?:截图)?缺失|这不是一道题|未找到可用的题目画面"
    r"|statement_latex|statement_plain|formula_latex|source_quote|teaching_notes"
    r"|shot_confidence|difficulties|key_points|pitfalls|\bsolution\b|\banswer\b"
    r"|knowledge\[\d+\]|examples\[\d+\]"
    r"|\bkp\d+\b|教师自编|本节课|本课"
    # 生成流水线自述。审核员常在总结末尾交代「回炉 1 轮后事实错误没有减少（28 → 31），
    # 已停止自动修订」——这是给写程序的人看的，老师只关心自己的教案哪里要改。
    r"|回炉|自动修订|事实错误没有减少|交叉审核|停止回炉"
    # 修订过程的痕迹。实测第 8 轮 ex2 的备注里留下这么一句：
    # 「原题干误录成与 ex1 相同的「sinα > 0」，已根据字幕与流程纠正为…」。
    # 这是在告诉老师"原题录错了又改对了"，属于制作过程，不是备课内容。
    r"|原题干|原题|误录|误转写|已根据字幕|据此纠正|流程纠正|已纠正为"
    r"|重新转写|改用字幕|按字幕改为|模型上一次|这一版)", re.I)

# 指向视频位置的秒数区间。必须挨着"视频"才算元信息——
# "先让学生独立想 10~20 秒"是教学建议，不能删。
_VIDEO_SPAN_NEAR = re.compile(
    r"(?:视频|片段|画面)[^\n]{0,8}?\d+\s*[–—\-~至到]\s*\d+\s*秒"
    r"|\d+\s*[–—\-~至到]\s*\d+\s*秒[^\n]{0,4}?(?:处|片段|画面|视频)")

# 句末标点：中文分号也算一句的结束（模型爱在一句里塞两件事）
_SENT_SPLIT = re.compile(r"(?<=[。！？；;])")


def _strip_meta_sentences(text: str) -> str:
    """丢掉整句都在讲"视频/系统/字段"的句子。

    为什么按句丢而不是按词替换：这类句子往往是「视频 02:45 段老师并未给出
    一道带数字的例题，而是……」，把时间戳抠掉会剩下一句没有主干的残句，
    读起来比整句删掉更糟。整句丢最干净。
    """
    if not text:
        return text
    kept = [s for s in _SENT_SPLIT.split(text)
            if s and not _META.search(s) and not _VIDEO_TS.search(s)
            and not _VIDEO_SPAN_NEAR.search(s)]
    return "".join(kept).strip()


# 一句话里去掉标点后还剩多少实词。少于这个数就当残句丢掉。
_RESIDUE_MIN = 4


def strip_meta_prose(text: str, min_chars: int = _RESIDUE_MIN) -> str:
    """逐句清洗 + 逐句丢弃残渣 + 重新拼回一整段。

    ``_strip_meta_sentences`` 只管「整句都是元信息就丢」，可审核总结这类文本
    常混着两种句子：一句正经话 + 一句被时间戳掏空后只剩「（）」这种标点的碎片。
    拼起来输出就会在段尾冒出一行孤零零的「）」。所以这里对每一句单独判空，
    碎句直接不参与拼接。
    """
    if not text:
        return ""
    kept: list[str] = []
    for sentence in _SENT_SPLIT.split(text):
        if not sentence or not sentence.strip():
            continue
        piece = _strip_meta_sentences(sentence)
        if not piece:
            continue
        solid = re.sub(r"[\s\W_]+", "", piece)
        if len(solid) < min_chars:
            continue
        kept.append(piece)
    return "".join(kept).strip()


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
    out = _strip_meta_sentences(clean(text))
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def meta_markers(text: str) -> list[str]:
    """体检用：这段文本里还残留哪几类元信息，干净则返回空列表。"""
    bad: list[str] = []
    if _VIDEO_TS.search(text):
        bad.append("时间戳")
    if _VIDEO_SPAN_NEAR.search(text):
        bad.append("视频秒数区间")
    hits = set(_META.findall(text))
    if hits:
        bad.append("视频分析口吻:" + "/".join(sorted(hits)[:3]))
    return bad


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