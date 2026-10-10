"""DOCX 导出自检：不依赖网络和模型，验证 Word 结构 + 真公式 + 图片 + 表格。"""
from pathlib import Path

from PIL import Image

from app.docx.builder import build_docx
from app.models import (Example, FlowStep, KnowledgePoint, Lesson, LessonMeta,
                        ReviewIssue, ReviewReport)

OUT = Path("selfcheck_out")
SHOTS = OUT / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

# 造两张假截图，确认图片能嵌进去
for name, color in (("ex1_c1.jpg", (250, 250, 245)), ("ex2_c1.jpg", (245, 248, 252)), ("kp1_board.jpg", (240, 240, 240))):
    Image.new("RGB", (1280, 720), color).save(SHOTS / name, quality=92)

lesson = Lesson(
    meta=LessonMeta(
        title="一元二次不等式（第 2 讲）—— 含参问题",
        subject="数学", grade="高一", duration_min=90,
        source_title="一元二次不等式串讲", uploader="某UP主",
        source_url="https://www.bilibili.com/video/BV1xx411c7XX",
        video_duration=1862.0, generated_at="2026-10-09 10:30:00",
    ),
    objectives=["理解一元二次不等式与二次函数的关系", "掌握含参问题的分类讨论方法", "培养数形结合的意识"],
    key_points=["解 $\\Delta=b^2-4ac\\ge 0$ 时不等式恒成立的条件", "两根的大小顺序与开口方向"],
    difficulties=["含参不等式中二次项系数为负时的分类讨论", "恒成立问题的最值转化"],
    prerequisites=["一元二次方程的根与系数关系", "函数的单调性"],
    materials=["草稿纸 2 张", "计算器（可选）"],
    knowledge=[
        KnowledgePoint(
            id="kp1", title="解的集合与二次函数的符号", ts_start=0, ts_end=420,
            summary="由 $x^2-2x-3<0$ 出发，把不等式问题转化为抛物线在 $x$ 轴下方的那段。",
            definition="当 $\\Delta>0$ 时，不等式 $ax^2+bx+c<0$ 的解集是 $(x_1,x_2)$。",
            formula_latex=r"\Delta = b^2-4ac = (-2)^2-4\cdot1\cdot(-3) = 16 > 0",
            key_points=["$\\Delta>0$ 时才有两根", "开口向上时解集在两根之间"],
            pitfalls=["忘记讨论 $a<0$ 的情况"],
            teaching_notes="先让学生自己画图，再问「什么时候点在抛物线下面」，让他们自己发现是两根之间。",
            source_quote="我们先把不等式和二次函数联系到一起",
            board_shot="kp1_board.jpg",
            examples=[Example(
                id="ex1", title="例1 解不等式", ts_start=200, ts_end=380,
                statement_latex="解下列不等式：$x^2-2x-3<0$。",
                statement_plain="解下列不等式。",
                answer="$x\\in(-1,3)$",
                solution=["因式分解：$x^2-2x-3=(x-3)(x+1)$",
                           "开口向上，两根为 $-1$ 与 $3$",
                           "取两根之间：$$\\boxed{x\\in(-1,3)}$$"],
                difficulty="基础", tags=["因式分解", "解集"], shot="ex1_c1.jpg",
                shot_confidence=0.93, verified=True,
            )],
        ),
        KnowledgePoint(
            id="kp2", title="含参问题的分类讨论", ts_start=420, ts_end=900,
            summary="对 $a$ 的符号与 $\\Delta$ 的符号依次讨论，归纳出四种情形。",
            definition="二次项系数 $a$ 决定开口方向，需单独讨论。",
            formula_latex=r"f(x)=ax^2+bx+c\ (a\neq 0),\qquad \Delta=b^2-4ac",
            key_points=["$a=0$ 退化为一次不等式", "$\\Delta<0$ 时恒正或恒负"],
            pitfalls=["漏掉 $a=0$ 的退化情况"],
            teaching_notes="画一张四格表让孩子填空，比逐条讲有效得多。",
            source_quote="下面我们来看含参的题目",
            board_shot="kp1_board.jpg",
            examples=[Example(
                id="ex2", title="例2 含参恒成立", ts_start=600, ts_end=880,
                statement_latex="若不等式 $x^2+2x+m<0$ 在 $x\\in[0,1]$ 上恒成立，求 $m$ 的取值范围。",
                statement_plain="若不等式在给定区间上恒成立，求 m 的取值范围。",
                answer="$m<-\\frac{3}{4}$",
                solution=["令 $f(x)=x^2+2x+m$，在 $[0,1]$ 上单调递增",
                           "只需 $f(1)<0$，即 $3+m<0$",
                           "解得 $$m<-\\frac{3}{4}$$"],
                difficulty="较难", tags=["恒成立", "单调性"], shot="ex2_c1.jpg",
                shot_confidence=0.88, verified=True,
            )],
        ),
    ],
    flow=[
        FlowStep(stage="复习导入", duration_min=10, activity="回顾一元二次方程的根", teacher_script="上次学的因式分解还记得吗？", intent="唤醒前置知识"),
        FlowStep(stage="新知讲解", duration_min=25, activity="讲解知识点 1", teacher_script="看图说话：什么时候点在抛物线下面？", intent="数形结合"),
        FlowStep(stage="例题精讲", duration_min=40, activity="例1、例2", teacher_script="先让学生做例1，我讲例2", intent="分层教学"),
        FlowStep(stage="课堂小结", duration_min=15, activity="梳理四类情形", teacher_script="我们一起填这张表", intent="结构化归纳"),
    ],
    common_mistakes=["忘记讨论 $a<0$", "恒成立问题写成恒不成立"],
    homework=["课后 3-1 第 2 题", "把例2 的条件改成 $a<0$ 再做一遍"],
    review=ReviewReport(
        verdict="revise", score=86, rounds=1,
        summary="整体结构完整，例2 的解析第 3 步与老师视频里的讲法略有出入，已按视频修订。",
        hallucination_risk="low",
        rule_checks=["✅ 规则体检通过（2 知识点 / 2 例题）"],
        issues=[ReviewIssue(severity="minor", target="knowledge[1].examples[0].solution[2]",
                            problem="解析第 3 步漏写定义域限制", suggestion="补充 $m<0$ 的验证")],
        teacher_notes="上课前务必让两位学生各做一遍例2 再讲解。",
    ),
)
lesson.stats = {"knowledge_count": 2, "example_count": 2, "verified_examples": 2}

out = build_docx(lesson, SHOTS, OUT / "自检教案.docx", OUT)
print("生成成功:", out, f"{out.stat().st_size / 1024:.1f} KB")

# 回读校验：OMML 公式对象数量、图片数量、表格数量
from docx import Document

doc = Document(str(out))
xml = doc.element.xml
print("Word 公式对象 (m:oMath) 数量:", xml.count("<m:oMath>") + xml.count("<m:oMath "))
print("独立公式段落 (m:oMathPara) 数量:", xml.count("<m:oMathPara"))
print("嵌入图片数量:", xml.count("<a:blip"))
print("表格数量:", len(doc.tables))
print("段落总数:", len(doc.paragraphs))
print("章节标题:", [p.text for p in doc.paragraphs if p.style.name == "Heading 1"])