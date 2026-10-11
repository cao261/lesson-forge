"""双版本导出自检 + 文本清洗自检。

重点验证三件事：
1. 学生版里绝对不能出现只属于老师的东西（讲法提示、易错、审核记录、时间戳）
2. 两份都不能出现 think / 代码围栏 / JSON 残留 / 不成对的 $
3. 公式在两份里都必须是原生 OMML
"""
from pathlib import Path
import re
import zipfile

from app.docx.builder import build_both
from app.docx.sanitize import clean, looks_dirty
from app.models import (Cue, Example, FlowStep, KnowledgePoint, Lesson,
                        LessonMeta, ReviewReport)

OUT = Path("selfcheck_out/two")

# ---------------------------------------------------------------- 文本清洗
print("=" * 74)
print("① 文本清洗：脏东西必须被洗掉，正常内容必须一字不动")
print("=" * 74)

CLEAN_CASES = [
    ("think 思考过程", "<think>让我想想…所以答案是 3。</think>所以答案是 3。",
     "所以答案是 3。"),
    ("未闭合 think", "正常前半段<think>后面被截断了", "正常前半段"),
    ("代码围栏包裹", "```json\n{\"a\": 1}\n```\n真正要写的正文", "真正要写的正文"),
    ("裸露 JSON 行", "标题\n{\"problem\": \"编造的问题\"}\n正文", "标题\n正文"),
    ("客套话开头", "好的，以下是本课知识点：", "以下是本课知识点："),
    ("不成对的 $", "公式是 $x^2-1<0 的解集", "公式是 x^2-1<0 的解集"),
    ("Markdown 加粗", "这是**重点**，不是别的", "这是重点，不是别的"),
    ("正常内容不受影响", "充分条件：P 是 Q 的充分条件 ⇔ A ⊆ B", "充分条件：P 是 Q 的充分条件 ⇔ A ⊆ B"),
    ("含转义美元不乱补", "价格是 \\$5 和 $x$ 两个变量", "价格是 \\$5 和 $x$ 两个变量"),
    ("以「这是」开头的正文不被误砍", "这是本节课的核心结论", "这是本节课的核心结论"),
    ("以「根据」开头的正文不被误砍", "根据集合的包含关系可得", "根据集合的包含关系可得"),
]
ok = 0
for name, raw, want in CLEAN_CASES:
    got = clean(raw)
    good = got == want
    print(f"  {'✅' if good else '❌'} {name}")
    if not good:
        print(f"       得到：{got!r}")
        print(f"       期望：{want!r}")
    ok += good
print(f"  → {ok}/{len(CLEAN_CASES)} 通过")
sanitize_ok = ok == len(CLEAN_CASES)

# ---------------------------------------------------------------- 造一份脏教案
DIRTY_SUMMARY = "<think>模型又在这思考了一堆…</think>老师讲的是集合包含关系。"
DIRTY_KEY = ["```json\n{\"x\": 1}\n```\n充分对应 A ⊆ B", "必要对应 B ⊆ A"]
DIRTY_SOLUTION = ["把两边都写成集合", "<think>补一句</think>用十字相乘分解因式", "$x^2-1<0$ 的解集"]


def make_lesson() -> Lesson:
    kp = KnowledgePoint(
        id="kp1", title="判断充分必要条件的方法",
        ts_start=9.0, ts_end=320.0,
        summary=DIRTY_SUMMARY,
        definition="若 $P$ 对应集合 $A$，$Q$ 对应 $B$，则 $P \\Rightarrow Q \\Leftrightarrow A \\subseteq B$。",
        formula_latex="P \\Rightarrow Q \\Leftrightarrow A \\subseteq B",
        key_points=list(DIRTY_KEY),
        pitfalls=["容易把包含方向记反"],
        teaching_notes="先让学生复述四种关系，再抛问题引出等价变形。",
        source_quote="充分不必要",
        examples=[Example(
            id="ex1", title="求参数范围",
            ts_start=60.0, ts_end=170.0,
            statement_latex="若 $-2<x<m$ 是 $x^2-x-6<0$ 的必要不充分条件，则 $m$ 的范围是____．",
            statement_plain="若 -2<x<m 是 x^2-x-6<0 的必要不充分条件，则 m 的范围是____．",
            answer="$m \\in (3, +\\infty)$",
            solution=list(DIRTY_SOLUTION),
            difficulty="中等", tags=["集合包含", "求参数"],
            shot="ex1_c1.jpg", verified=True,
            notes="让学生先自己设 A、B 两个集合",
        ), Example(
            id="ex2", title="概念辨析题",
            ts_start=200.0, ts_end=260.0,
            statement_latex="下列正确的是（ ）",
            statement_plain="下列正确的是（ ）",
            answer="B", solution=[],           # 老师没讲过程
            difficulty="基础", tags=["概念辨析"],
            shot="ex1_c1.jpg", verified=True,
        )],
    )
    return Lesson(
        meta=LessonMeta(title="充分必要条件", subject="数学", grade="高一",
                        duration_min=90.0, source_title="测试课", video_duration=3600.0,
                        generated_at="2026-10-09 12:00:00"),
        objectives=["会判断四种关系"],
        key_points=["等价变形法"], difficulties=["包含方向"],
        materials=["白板"],
        knowledge=[kp],
        flow=[FlowStep(stage="导入", duration_min=5, activity="回顾", teacher_script="你们还记得四种关系吗", intent="激活旧知")],
        common_mistakes=["包含方向记反"],
        homework=["照例题1改编一题"],
        review=ReviewReport(verdict="revise", score=88, rounds=1, summary="整体可用",
                            hallucination_risk="low", model_review_ok=True),
    )


# ---------------------------------------------------------------- 导出
print()
print("=" * 74)
print("② 双版本导出")
print("=" * 74)
lesson = make_lesson()
OUT.mkdir(parents=True, exist_ok=True)
teacher, student = build_both(lesson, OUT, OUT, "t.docx", "s.docx")
print(f"  教师备课版：{teacher.name}（{teacher.stat().st_size / 1024:.1f} KB）")
print(f"  学生讲义  ：{student.name}（{student.stat().st_size / 1024:.1f} KB）")

from docx import Document


def text_of(path: Path) -> str:
    return "\n".join(p.text for p in Document(path).paragraphs)


def omml_count(path: Path) -> int:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return len(re.findall(r"<m:oMath[ >]", xml))


def omml_text(path: Path) -> str:
    """把公式对象里的文字抠出来，才能验证公式内容对不对。"""
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml").decode("utf-8")
    return " ".join(re.findall(r"<m:t[^>]*>([^<]*)</m:t>", xml))


t_text, s_text = text_of(teacher), text_of(student)
t_math, s_math = omml_text(teacher), omml_text(student)

print()
print("  ── 学生版该有的 ──")
MUST_HAVE_S = ["一、课堂同步精讲与针对演练", "三、参考答案与分步解析", "ex1．", "ex2．", "答案：", "充分对应 A ⊆ B"]
for kw in MUST_HAVE_S:
    hit = kw in s_text
    print(f"    {'✅' if hit else '❌'} {kw}")
student_has = all(kw in s_text for kw in MUST_HAVE_S)

# 答案里的公式是 OMML 对象，正文里查不到，要到公式对象里查
ans_math_ok = "(3" in s_math.replace(" ", "") and "+∞" in s_math
print(f"    {'✅' if ans_math_ok else '❌'} 答案公式 m∈(3,+∞) 渲染成原生公式（公式内文字：{s_math[:60]!r}）")
student_has = student_has and ans_math_ok

print()
print("  ── 学生版绝不该有的（只属于老师的）──")
MUST_NOT_S = {
    "讲法提示": "【备课点拨与授课启发】",
    "易错/注意": "注意／易错",
    "审核记录": "审核记录",
    "教学目标": "教学目标",
    "课堂流程": "课堂流程",
    "字幕依据": "字幕依据",
    "视频位置时间戳": "视频位置",
    "解析步骤": "【规范解法与分步剖析】",
    "板书字幕依据引号": "字幕依据",
    "只报答案的提示": "只报了答案",
}
clean_student = True
for name, kw in MUST_NOT_S.items():
    bad = kw in s_text
    print(f"    {'✅' if not bad else '❌'} 不含「{kw}」（{name}）")
    clean_student = clean_student and not bad

print()
print("  ── 教师版该有的 ──")
MUST_HAVE_T = ["一、教学目标", "二、教学重点与难点",
               "三、核心知识精讲与针对典例剖析（讲练一体）",
               "四、全课核心思想与易错点汇总", "五、教学准备与教具",
               "六、课堂流程与时间分配建议", "七、课后作业与巩固练习",
               "附、教案审核与备课说明",
               "【规范解法与分步剖析】", "【备课点拨与授课启发】"]
teacher_has = True
for kw in MUST_HAVE_T:
    hit = kw in t_text
    print(f"    {'✅' if hit else '❌'} {kw}")
    teacher_has = teacher_has and hit

print()
print("  ── 只报答案的例题，两份的处理 ──")
# builder 现在对"没有可展示的分步解析"的例题写这句提示
NO_SOLUTION_HINT = "视频中主要为教师口头点拨与引导分析"
SOL_LABEL = "【规范解法与分步剖析】"
print(f"    {'✅' if NO_SOLUTION_HINT in t_text else '❌'} 教师版如实说明该题视频里没展开推导")
print(f"    {'✅' if SOL_LABEL not in s_text else '❌'} 学生版没有硬塞解析")
note_ok = (NO_SOLUTION_HINT in t_text) and SOL_LABEL not in s_text

print()
print("  ── 脏东西检查 ──")
DIRTY_MARKERS = ["<think>", "</think>", "```", '{"x"', '"problem"']
for label, txt in (("教师版", t_text), ("学生版", s_text)):
    hits = [m for m in DIRTY_MARKERS if m in txt]
    print(f"    {'✅' if not hits else '❌'} {label}：{hits or '干净'}")
    if hits:
        print(f"        上下文：{[l for l in txt.splitlines() if any(m in l for m in hits)][:2]}")

print()
print("  ── 公式 ──")
for label, path in (("教师版", teacher), ("学生版", student)):
    n = omml_count(path)
    print(f"    {'✅' if n > 0 else '❌'} {label} 原生公式 {n} 个")

print()
print("  ── 体量对比 ──")
tp, sp = len(t_text), len(s_text)
print(f"    教师版 {tp} 字 ／ 学生版 {sp} 字 ／ 学生版占 {sp / tp:.0%}")
size_ok = sp < tp * 0.6

print()
print("=" * 74)
all_ok = (sanitize_ok and student_has and clean_student and teacher_has
           and note_ok and size_ok
           and not any(m in t_text or m in s_text for m in DIRTY_MARKERS)
           and omml_count(teacher) > 0 and omml_count(student) > 0)
print("总计：" + ("全部通过 ✅" if all_ok else "有失败项 ❌"))
print("=" * 74)