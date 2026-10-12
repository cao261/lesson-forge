# -*- coding: utf-8 -*-
"""自检：教案正文里不能出现"视频分析口吻"。

教案的读者是**看过视频的老师本人**。正文里出现「视频 02:45 段老师讲的是…」
「视频位置 0–37s」「kp5 已知」「截图未获取到」，等于把制作过程写给不需要知道的人看，
老师读完只会觉得这教案没做好。

参考文档（用户提供的合格样例）里一个都没有。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.docx.sanitize import _strip_meta_sentences, meta_markers, strip_meta_prose

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


# ---------------------------------------------------------------- 必须被清掉
DIRTY = [
    "视频 02:45–03:34 段老师并未给出一道带具体数字的例题，而是用一般化的 A、B 推导斜率公式。",
    "视频位置：0–37s",
    "老师原话：你取到 0 的时候 sin0 应该正好等于 0",
    "截图未获取到、题干未转写成功。",
    "由 kp5 已知：当 α ≠ 90° 时，倾斜角与斜率一一对应。",
    "source_quote 引用准确，statement_latex 也对得上。",
    "知识[2].examples[1] 这条例题有问题。",
    "本节课老师没有布置作业。",
    "这是一道教师自编的题。",
    "视频里老师没有出可做的题，这段属于口头讲解。",
    # 实测第 8 轮 ex2 的备注里留下的制作痕迹：告诉老师"原题录错了又改对了"
    "原题干误录成与 ex1 相同的「sinα > 0」，已根据字幕与流程纠正为「斜率相等则倾斜角相等」。",
    "这一版的题干是重新转写的，模型上一次给的版本不对。",
    "已据此纠正为标准表述。",
]
for raw in DIRTY:
    out = _strip_meta_sentences(raw)
    check(f"清掉：{raw[:26]}…", out == "", f"剩「{out[:40]}」")

# ---------------------------------------------------------------- 必须保留
KEEP = [
    "取 α=0 检验：sin0=0，不大于 0，故命题不成立。",
    "分子分母同序对应：可以用 (y₂-y₁) 比 (x₂-x₁)，也可以反过来，但绝不能减反。",
    "先让学生独立想 10~20 秒，再让一位学生说出对错和理由。",   # 教学建议里的秒数
    "倾斜角的取值范围是 [0°,180°)，0 能取到，180° 取不到。",
    "两条直线斜率相等，k₁=k₂，在正切函数单调区间内可得 α₁=α₂。",
]
for raw in KEEP:
    out = _strip_meta_sentences(raw)
    check(f"保留：{raw[:26]}…", out == raw, f"变成「{out[:40]}」")

# ---------------------------------------------------------------- 混合文本
mixed = "取 α=0 检验。视频 02:45 段老师并未给出例题。斜率 k=tanα。"
out = _strip_meta_sentences(mixed)
check("混合文本只删脏句、留正文", "sin" not in out and "视频" not in out and "tan" in out, out)

# ---------------------------------------------------------------- 体检函数
check("meta_markers 能报出时间戳", "时间戳" in meta_markers("发生在 02:45 的时候"))
check("meta_markers 能报出 kp 编号", any("kp" in m for m in meta_markers("由 kp5 已知")))
check("meta_markers 对干净文本返回空", meta_markers("斜率 k=tanα。") == [])

# ---------------------------------------------------------------- 内部字段路径要变人话
from app.docx.builder import _human_target

TARGETS = [
    ("knowledge[2].examples[1].solution（ex8）", "ex8 · 解析步骤"),
    ("knowledge[1].examples[0].statement_latex（ex3）", "ex3 · 题干"),
    ("knowledge[0].formula_latex", "公式"),
    ("knowledge[2].teaching_notes（kp4）", "kp4 · 备课点拨"),
    ("knowledge[1].answer（ex5）", "ex5 · 参考答案"),
]
for raw, want in TARGETS:
    got = _human_target(raw)
    check(f"路径变人话：{raw[:34]}", got == want, f"得到「{got}」")

check("空 target 返回空串（调用方自己拼，不要凭空加字）", _human_target("") == "")
check("已经是人话的 target 原样保留", _human_target("[ex1]") == "[ex1]",
      "重复套括号会让 ex1 出现两次")
check("人话里不残留 knowledge[", "knowledge[" not in _human_target("knowledge[2].examples[1].solution（ex8）"))
check("裸下标目标也变人话", _human_target("difficulties[2]") == "难点 · 第 3 项",
      f"得到「{_human_target('difficulties[2]')}」")

# ---------------------------------------------------------------- 英文字段名要变中文
from app.docx.builder import _RISK_CN, _say_field_words

SAY = [
    ("examples 部分诚实地标注为非题型", "例题部分诚实地标注为非题型"),
    ("但 solution 应为空", "但解析步骤应为空"),
    ("difficulties[2] 出现一处笔误", "难点[2] 出现一处笔误"),
    ("teaching_notes 这段写得好", "备课点拨这段写得好"),
]
for raw, want in SAY:
    got = _say_field_words(raw)
    check(f"字段名变中文：{raw[:20]}", got == want, f"得到「{got}」")

check("编造风险的英文枚举变中文", _RISK_CN["medium"] == "中")
check("字段名翻译不会误伤数学内容", _say_field_words("求解 x=1 时的 answer") == "求解 x=1 时的参考答案")

# ---------------------------------------------------------------- 残句必须被丢弃
# 实测故障：审核总结净化后，段尾冒出一行孤零零的「）」——那一整句被时间戳掏空，
# 只剩括号和标点。段级判空挡不住它（整段还有别的句子），必须逐句判空。
RESIDUE = [
    ("（视频 02:45 段老师只口述没有板书）。建议老师在课前过一遍。", "建议老师在课前过一遍。"),
    ("上面的结论成立。视频 03:10 处给出的答案有误。", "上面的结论成立。"),
    ("全段都是脏话 02:45 视频位置。", ""),
]
for raw, want in RESIDUE:
    got = strip_meta_prose(raw)
    check(f"残句丢弃：{raw[:22]}…", got == want, f"得到「{got}」")

check("流水线自述被丢弃（回炉/自动修订）",
      strip_meta_prose("结论成立。⚠ 回炉 1 轮后事实错误没有减少（28 → 31），已停止自动修订。")
      == "结论成立。")
# 实测第 8 轮：修订痕迹夹在正常的教学提示中间，要整句丢、正文留
MIXED = [
    ("原题干误录成与 ex1 相同的「sinα > 0」，已根据字幕纠正为「斜率相等则倾斜角相等」。"
     "先看定义域。", "先看定义域。"),
    ("本题的核心是抓住 α=0 这个边界情形，学生最容易在这里丢分。"
     "原题误转写了，已重新转写。", "本题的核心是抓住 α=0 这个边界情形，学生最容易在这里丢分。"),
]
for raw, want in MIXED:
    got = strip_meta_prose(raw)
    check(f"修订痕迹整句丢弃、正文保留：{raw[:18]}…", got == want, f"得到「{got}」")

check("纯标点残渣被丢弃", strip_meta_prose("斜率定义正确。（）。") == "斜率定义正确。",
      f"得到「{strip_meta_prose('斜率定义正确。（）。')}」")
check("strip_meta_prose 对空串安全", strip_meta_prose("") == "")
check("strip_meta_prose 结果仍然是干净正文",
      meta_markers(strip_meta_prose("结论成立。视频 02:45 段有误。")) == [])

# ---------------------------------------------------------------- 排版器自己的文案不许被清
# 净化层只针对模型文本。像「▸ 提示：视频中主要为教师口头点拨…」这种
# 固定提示语是**必须出现**的说明，被误清就是缺陷（实测真发生过）。
import tempfile
from docx import Document

from app.docx.builder import DocxBuilder
from app.models import Example, KnowledgePoint, Lesson, LessonMeta, ReviewIssue

FIXED_HINT = "视频中主要为教师口头点拨与引导分析"
FIXED_SUPP = "补充建议（1 条，非错误，视频里没讲但老师通常会自己补）"
FIXED_DROP = "（另有 1 条意见针对的是老师口头讲解的段落，这些段落不是例题，已从清单中移除。）"

_tmp = Lesson(
    meta=LessonMeta(title="元信息旁路测试"),
    knowledge=[KnowledgePoint(id="kp1", title="知识点", definition="定义", examples=[
        Example(id="ex1", title="例题", statement_plain="题面", answer="答案"),
    ])],
)
# 例题没有分步解析 → 排版器会写那句固定提示语，走的正是 trusted 旁路
_tmp.knowledge[0].examples[0].solution = []
# 一条已降级成口述要点的 ex2，审核对它提的意见应当被整条剔除
_tmp.knowledge[0].examples.append(
    Example(id="ex2", title="口述要点", statement_plain="", answer="", is_exercise=False)
)
_tmp.review.model_review_ok = True
_tmp.review.verdict = "revise"
_tmp.review.score = 80
_tmp.review.rounds = 1
_tmp.review.summary = "（视频 02:45 段老师只口述没有板书）。斜率与倾斜角的对应关系已经讲透。"
_tmp.review.hallucination_risk = "作业全部来自视频原题。"
_tmp.review.teacher_notes = "建议课堂上再强调一次定义域。"
_tmp.review.rule_checks = ["[ex1] 题干与截图一致。", "[ex2] 截图未获取到。"]
_tmp.review.issues = [
    ReviewIssue(kind="supplement", severity="minor", target="knowledge[0].examples[1].notes",
                problem="这一点可以再讲细一点", suggestion="补一个具体例子。"),
    ReviewIssue(kind="fact", severity="major", target="ex2",
                problem="ex2 题干为空", suggestion="补题干。"),
]

_td = Path(tempfile.gettempdir())
b = DocxBuilder(_tmp, _td, _td, audience="teacher")
b._render_example_teacher(_tmp.knowledge[0].examples[0])   # 触发固定提示语
b._review()                                             # 触发审核附录三段固定文案

_docx_path = _td / "lf_meta_bypass.docx"
b.doc.save(_docx_path)
_paras = [p.text for p in Document(_docx_path).paragraphs]
_text = "\n".join(_paras)
_punct_only = [t for t in _paras
               if t.strip() and not re.sub(r"[\s\W_]+", "", t).strip()]
check("固定提示语不被净化层误清（视频中口头点拨）", FIXED_HINT in _text)
check("审核附录：补充建议标题在", FIXED_SUPP in _text)
check("审核附录：过期意见说明在", FIXED_DROP in _text)
check("审核附录：真意见的正文还在", "这一点可以再讲细一点" in _text)
check("审核附录：过期意见整条剔除", "ex2 题干为空" not in _text)
check("审核附录：已降级例题的体检条目被剔除", "截图未获取到" not in _text)
check("审核附录：没有只剩标点的残行", not _punct_only, f"残行：{_punct_only}")
# 原缺陷：审核意见那行以「）」结尾。判据要看这一行本身，
# 不能扫全文找括号——「补充建议（1 条…）」里的括号是正常的。
_opinion = [t for t in _paras if t.startswith("审核意见：")]
check("审核意见只剩干净的一句", _opinion == ["审核意见：斜率与倾斜角的对应关系已经讲透。"],
      f"得到 {_opinion}")
check("审核总结的时间戳那句没漏进正文", "（视频 02:45" not in _text)
check("审核总结里的正经话保留", "斜率与倾斜角的对应关系已经讲透。" in _text)
_docx_path.unlink(missing_ok=True)

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails[:6]))
    raise SystemExit(1)
print("✅ 教案正文里没有视频分析口吻")