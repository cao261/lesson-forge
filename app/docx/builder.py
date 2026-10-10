"""把 Lesson 渲染成 Word 文档（.docx）。

关键点：公式不是贴图，是真正的 Word 公式对象（OMML）——在 Word 里可以直接点进去改。
文本里用 $...$ 表示行内公式、$$...$$ 表示独立成行的公式。
"""
from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from app.docx.omml import latex_to_image, latex_to_omml, split_lines, split_math
from app.docx.sanitize import clean, clean_block
from app.models import Lesson, fmt_ts

BODY_FONT = "宋体"
HEAD_FONT = "微软雅黑"
LATIN_FONT = "Times New Roman"

MATH_PARA_HEAD = '<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>'


class DocxBuilder:
    """audience 决定这份文档给谁看：

    - ``teacher`` 教师备课版：知识点讲全、解析分步、附讲法提示与易错点、末尾附审核记录。
    - ``student`` 学生版：只有粗略知识点和题目（题干原图 + 答案），
      任何"讲法提示""易错提醒""审核记录"都不出现——那些是老师自己看的，
      端给学生只会让他分心。
    """

    def __init__(self, lesson: Lesson, shots_dir: Path, work_dir: Path,
                 audience: str = "teacher") -> None:
        self.lesson = lesson
        self.shots_dir = shots_dir
        self.audience = audience
        self.formula_dir = work_dir / "_formulas"
        self.formula_dir.mkdir(parents=True, exist_ok=True)
        self._formula_seq = 0
        self.doc = Document()
        self._setup_styles()

    @property
    def is_student(self) -> bool:
        return self.audience == "student"

    @staticmethod
    def _c(text: str) -> str:
        """所有进正文的文本都过一遍清洗，挡住 think/围栏/JSON 残留。"""
        return clean_block(text) if text else ""

    # ------------------------------------------------------------------ 样式
    def _setup_styles(self) -> None:
        normal = self.doc.styles["Normal"]
        normal.font.name = LATIN_FONT
        # 学生版是端着平板、坐在一起看的，字和行距都要松一档；
        # 教师版是自己盯的稿子，紧一点更好读。
        normal.font.size = Pt(12 if self.is_student else 10.5)
        normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
        pf = normal.paragraph_format
        pf.space_after = Pt(7 if self.is_student else 4)
        pf.line_spacing = 1.6 if self.is_student else 1.35

        scale = 1.12 if self.is_student else 1.0
        for name, size, color in (
            ("Heading 1", 15, 0x1F3864),
            ("Heading 2", 13, 0x1F4E79),
            ("Heading 3", 11.5, 0x2E5C8A),
        ):
            style = self.doc.styles[name]
            style.font.name = LATIN_FONT
            style.font.size = Pt(round(size * scale, 1))
            style.font.bold = True
            style.font.color.rgb = RGBColor((color >> 16) & 255, (color >> 8) & 255, color & 255)
            style.element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
            style.paragraph_format.space_before = Pt(12 if self.is_student else 10)
            style.paragraph_format.space_after = Pt(7 if self.is_student else 5)

        section = self.doc.sections[0]
        # 学生版页边距宽一点，平板上看图更满
        margin = 1.0 if self.is_student else 0.9
        section.left_margin = Inches(margin)
        section.right_margin = Inches(margin)
        section.top_margin = Inches(0.85)
        section.bottom_margin = Inches(0.85)

    # ------------------------------------------------------------------ 基础工具
    def _p(self, text: str = "", style: str | None = None, align=None, size: float | None = None,
           bold: bool = False, color: RGBColor | None = None, indent: float | None = None):
        para = self.doc.add_paragraph(style=style)
        if align is not None:
            para.alignment = align
        if indent is not None:
            para.paragraph_format.left_indent = Inches(indent)
        if text:
            self.add_rich(para, text, size=size, bold=bold, color=color)
        return para

    def _bullet(self, text: str, level: int = 0):
        para = self.doc.add_paragraph(style="List Bullet")
        para.paragraph_format.left_indent = Inches(0.28 + 0.25 * level)
        para.paragraph_format.space_after = Pt(2)
        self.add_rich(para, text)
        return para

    def _image(self, filename: str | None, caption: str = "", max_width: float = 5.6):
        if not filename:
            return
        path = self.shots_dir / filename
        if not path.exists():
            return
        try:
            para = self.doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.space_before = Pt(4)
            para.paragraph_format.space_after = Pt(2)
            para.add_run().add_picture(str(path), width=Inches(max_width))
        except Exception:  # noqa: BLE001
            return
        if caption:
            cap = self.doc.add_paragraph()
            cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = cap.add_run(caption)
            run.font.size = Pt(8.5)
            run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            run.font.name = LATIN_FONT
            run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    # ------------------------------------------------------------------ 混排 + 公式
    def add_rich(self, para, text: str, size: float | None = None, bold: bool = False,
                 color: RGBColor | None = None):
        """把含 $...$ / $$...$$ 的文本写进段落，公式转成 Word 公式对象。"""
        if not text:
            return
        for kind, body, _raw in split_math(text):
            if kind == "text":
                run = para.add_run(body)
                run.font.name = LATIN_FONT
                run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
                if size:
                    run.font.size = Pt(size)
                if bold:
                    run.font.bold = True
                if color:
                    run.font.color.rgb = color
            else:
                self._add_math(para, body, block=(kind == "block"), size=size)
        return para

    def _add_math(self, para, latex: str, block: bool = False, size: float | None = None) -> None:
        lines = split_lines(latex) or [latex]
        # 位图兜底时要自己定字号；OMML 走 Word 自身的渲染，会跟着段落样式走
        size = size or (12.0 if self.is_student else 10.5)
        for line in lines:
            omml = latex_to_omml(line)
            if omml:
                self._insert_omml(para, omml, block=block)
            else:
                self._insert_formula_image(para, line, size=size)

    def _insert_omml(self, para, omml: str, block: bool) -> None:
        try:
            if not block:
                para._p.append(parse_xml(omml))
                return
            # 独立公式：剥出 oMath 内部内容，包一层 oMathPara 让它在 Word 里居中
            open_end = omml.find(">")
            close_at = omml.rfind("</m:oMath>")
            if open_end == -1 or close_at == -1:
                para._p.append(parse_xml(omml))
                return
            inner = omml[open_end + 1 : close_at]
            para._p.append(parse_xml(MATH_PARA_HEAD + inner + "</m:oMathPara>"))
        except Exception:  # noqa: BLE001
            # 万一 OMML 结构异常，至少别把整份文档搞挂
            run = para.add_run("〔公式转换异常〕")
            run.font.size = Pt(9)
            run.font.color.rgb = RGBColor(0x99, 0x33, 0x33)

    def _insert_formula_image(self, para, latex: str, size: float | None = None) -> None:
        """OMML 转换失败时的兜底：渲染成高清 PNG 贴进去。"""
        self._formula_seq += 1
        target = self.formula_dir / f"eq_{self._formula_seq:03d}.png"
        made = latex_to_image(latex, target, fontsize=int((size or 10.5) * 2))
        if made:
            try:
                run = para.add_run()
                run.add_picture(str(made), height=Pt((size or 10.5) * 1.6))
                return
            except Exception:  # noqa: BLE001
                pass
        # 最后一招：至少把公式原文留在文档里，别让人丢内容
        run = para.add_run(f"〔公式：{latex}〕")
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x99, 0x33, 0x33)
        run.font.name = LATIN_FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    # ------------------------------------------------------------------ 各章节
    def build(self, out_path: Path) -> Path:
        L = self.lesson
        meta = L.meta

        title = self._p(align=WD_ALIGN_PARAGRAPH.CENTER)
        run = title.add_run(meta.title or "教案")
        run.font.size = Pt(20)
        run.font.bold = True
        run.font.name = LATIN_FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
        run.font.color.rgb = RGBColor(0x1F, 0x38, 0x64)

        sub = self._p(align=WD_ALIGN_PARAGRAPH.CENTER, size=9, color=RGBColor(0x77, 0x77, 0x77))
        if self.is_student:
            sub.add_run(f"配套讲义 · {meta.subject} {meta.grade} · 生成时间 {meta.generated_at}")
        else:
            sub.add_run(f"教师备课版 · 由 {meta.generator} 依据教学视频自动生成 · {meta.generated_at}")

        if self.is_student:
            self._student_head()
            self._p("一、本课知识点", style="Heading 1")
            self._knowledge_student()
            if L.example_count():
                self._p("二、例题", style="Heading 1")
                self._examples_student()
            if L.homework:
                self._p("三、课后作业", style="Heading 1")
                self._homework_items()
        else:
            self._info_table()
            self._p("一、本课知识点", style="Heading 1")
            self._knowledge_teacher()
            if L.example_count():
                self._p("二、例题精讲", style="Heading 1")
                self._examples_teacher()
            self._objectives()
            self._key_difficulties()
            self._common_mistakes()
            self._materials()
            self._flow_table()
            self._homework()
            self._review()
            self._footer_note()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out_path))
        return out_path

    # ------------------------------------------------------------------ 学生版
    def _student_head(self) -> None:
        """学生版顶部一行小字，说清这份讲义是干什么用的。"""
        meta = self.lesson.meta
        bits = [b for b in (meta.subject, meta.grade) if b]
        head = self._p(align=WD_ALIGN_PARAGRAPH.CENTER, size=10, color=RGBColor(0x55, 0x55, 0x55))
        head.add_run("　".join(bits) + "　·　先自己看例题，不会的地方问老师")
        head.paragraph_format.space_after = Pt(10)

    def _knowledge_student(self) -> None:
        """学生只看要点和公式：不需要讲法、不需要易错提醒、不需要时间戳。"""
        for idx, kp in enumerate(self.lesson.knowledge, 1):
            self._p(f"{idx}. {self._c(kp.title)}", style="Heading 2")

            definition = self._c(kp.definition)
            if definition:
                self._p(definition, indent=0.15)

            if kp.formula_latex:
                math_para = self.doc.add_paragraph()
                math_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                self._add_math(math_para, self._c(kp.formula_latex), block=True)

            for item in kp.key_points:
                self._bullet(self._c(item))

            if not kp.key_points and not definition and not kp.formula_latex:
                self._p(self._c(kp.summary), indent=0.15)

            board = kp.board_shot if (kp.board_shot and self.shots_dir.joinpath(kp.board_shot).exists()) else None
            self._image(board, "板书要点")
            if kp.examples:
                self._p(f"本节例题：{'、'.join(ex.title for ex in kp.examples)}",
                        size=9, color=RGBColor(0x88, 0x88, 0x88), indent=0.15)

    def _examples_student(self) -> None:
        """学生版例题：原图 + 答案。没有解析就不硬塞——老师只讲了答案就是只有答案。"""
        for kp in self.lesson.knowledge:
            for ex in kp.examples:
                self._p(f"{ex.id}．{self._c(ex.title)}", style="Heading 2")
                for line in (self._c(ex.statement_latex or ex.statement_plain)).split("\n"):
                    if line.strip():
                        self._p(line.strip(), indent=0.2)
                self._image(ex.shot, "题目原图")
                if ex.answer:
                    self._p(f"答案：{self._c(ex.answer)}", size=11, bold=True, indent=0.2)

    def _info_table(self) -> None:
        L = self.lesson
        meta = L.meta
        verdict = {"pass": "通过", "revise": "需微调（已标注）", "fail": "需重审"}.get(L.review.verdict, L.review.verdict)
        rows = [
            ("学科", meta.subject or "—"),
            ("年级", meta.grade or "—"),
            ("建议课时", f"{meta.duration_min:.0f} 分钟"),
            ("授课教师", meta.teacher or "＿＿＿＿＿＿"),
            ("学生", meta.student or "＿＿＿＿＿＿"),
            ("视频来源", meta.source_title or "—"),
            ("UP 主", meta.uploader or "—"),
            ("视频时长", fmt_ts(meta.video_duration)),
            ("原视频链接", meta.source_url or "—"),
            ("审核结论", f"{verdict}（{L.review.rounds} 轮回炉）"),
        ]
        table = self.doc.add_table(rows=0, cols=4)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i in range(0, len(rows), 2):
            pair = rows[i : i + 2]
            cells = table.add_row().cells
            for j in range(2):
                label, value = pair[j]
                _set_cell(cells[j * 2], label, bold=True, shade="EEF3FA")
                _set_cell(cells[j * 2 + 1], value)
        for row in table.rows:
            row.cells[0].width = Inches(0.8)
            row.cells[1].width = Inches(2.5)
        self.doc.add_paragraph()

    def _objectives(self) -> None:
        if not self.lesson.objectives:
            return
        self._p("三、教学目标", style="Heading 1")
        for item in self.lesson.objectives:
            self._bullet(self._c(item))

    def _key_difficulties(self) -> None:
        self._p("四、教学重点与难点", style="Heading 1")
        if self.lesson.key_points:
            self._p("教学重点", size=10.5, bold=True)
            for item in self.lesson.key_points:
                self._bullet(self._c(item))
        if self.lesson.difficulties:
            self._p("教学难点", size=10.5, bold=True)
            for item in self.lesson.difficulties:
                self._bullet(self._c(item))
        if self.lesson.prerequisites:
            self._p("前置知识（学生需先掌握）", size=10.5, bold=True)
            for item in self.lesson.prerequisites:
                self._bullet(self._c(item))

    def _materials(self) -> None:
        if not self.lesson.materials:
            return
        self._p("六、教学准备", style="Heading 1")
        for item in self.lesson.materials:
            self._bullet(self._c(item))

    def _knowledge_teacher(self) -> None:
        """教师版知识点：讲了什么、规范表述、公式、要点、易错、讲法提示、字幕依据。"""
        for idx, kp in enumerate(self.lesson.knowledge, 1):
            self._p(f"{idx}. {self._c(kp.title)}", style="Heading 2")
            meta_line = self._p(size=9, color=RGBColor(0x88, 0x88, 0x88))
            meta_line.add_run(f"视频位置：{fmt_ts(kp.ts_start)} – {fmt_ts(kp.ts_end)}")
            if kp.summary:
                self._p(f"讲了什么：{self._c(kp.summary)}")
            if kp.definition:
                self._p(f"定义／结论：{self._c(kp.definition)}", indent=0.15)
            if kp.formula_latex:
                self._p("核心公式：", size=10.5, bold=True, indent=0.15)
                math_para = self.doc.add_paragraph()
                math_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                self._add_math(math_para, self._c(kp.formula_latex), block=True)
            if kp.key_points:
                self._p("要点：", size=10.5, bold=True, indent=0.15)
                for item in kp.key_points:
                    self._bullet(self._c(item))
            if kp.pitfalls:
                self._p("注意／易错：", size=10.5, bold=True, indent=0.15)
                for item in kp.pitfalls:
                    self._bullet(self._c(item))
            if kp.teaching_notes:
                note = self._p(size=10.5, indent=0.15)
                head = note.add_run("▸ 讲法提示：")
                head.font.bold = True
                head.font.color.rgb = RGBColor(0xB0, 0x60, 0x00)
                head.font.name = LATIN_FONT
                head._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
                self.add_rich(note, self._c(kp.teaching_notes))
            if kp.source_quote:
                quote = self._p(f"字幕依据：「{self._c(kp.source_quote)}」", size=9,
                                color=RGBColor(0x99, 0x99, 0x99), indent=0.15)
                quote.paragraph_format.space_after = Pt(8)
            if kp.board_shot:
                self._image(kp.board_shot, "板书要点（本知识点总结画面）")
            if kp.examples:
                self._p("本节例题", size=10.5, bold=True, indent=0.15)
                for ex in kp.examples:
                    self._example_preview(ex)

    def _example_preview(self, ex) -> None:
        """知识点后面的例题速览：题干 + 原图 + 答案，详细解析见「例题精讲」。"""
        head = self.doc.add_paragraph()
        head.paragraph_format.left_indent = Inches(0.3)
        head.paragraph_format.space_before = Pt(6)
        run = head.add_run(f"{ex.id}．{ex.title}")
        run.font.bold = True
        run.font.size = Pt(10)
        run.font.name = LATIN_FONT
        run._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
        tag = self._p(
            f"[{fmt_ts(ex.ts_start)}]{' · ' + '、'.join(ex.tags) if ex.tags else ''}"
            f"{' · ' + ex.difficulty if ex.difficulty else ''}",
            size=8.5, color=RGBColor(0x88, 0x88, 0x88), indent=0.3,
        )
        tag.paragraph_format.space_after = Pt(1)
        if not ex.verified:
            self._p("⚠ 题干未经画面确认，请对照视频复核。", size=8.5,
                    color=RGBColor(0xC0, 0x39, 0x2B), indent=0.3)
        body = ex.statement_latex or ex.statement_plain
        for line in (body or "").split("\n"):
            if line.strip():
                self._p(line.strip(), size=10, indent=0.35)
        self._image(ex.shot, f"{ex.id} 题目原图", max_width=4.6)
        if ex.answer:
            self._p(f"答案：{ex.answer}", size=10, indent=0.35)
        more = self._p("（完整分步解析见「二、例题精讲」）", size=8.5,
                       color=RGBColor(0x99, 0x99, 0x99), indent=0.35)
        more.paragraph_format.space_after = Pt(2)

    def _examples_teacher(self) -> None:
        """教师版例题：原图 + 分步解析 + 答案 + 讲解要点。

        老师只报答案没讲过程的，解析就是空的——这里尊重字幕事实，不替老师编。
        """
        for kp in self.lesson.knowledge:
            for ex in kp.examples:
                head = f"{ex.id}．{self._c(ex.title)}"
                self._p(head, style="Heading 2")
                tags = "　".join(filter(None, [
                    f"[{fmt_ts(ex.ts_start)}–{fmt_ts(ex.ts_end)}]",
                    f"难度：{ex.difficulty}" if ex.difficulty else "",
                    f"考点：{'、'.join(ex.tags)}" if ex.tags else "",
                ]))
                meta_line = self._p(size=9, color=RGBColor(0x88, 0x88, 0x88))
                meta_line.add_run(tags)
                if not ex.verified:
                    warn = self._p(
                        "⚠ 该题截图未通过画面确认，题干可能不完整，请对照视频复核。",
                        size=9, color=RGBColor(0xC0, 0x39, 0x2B), bold=True, indent=0.15,
                    )
                    warn.paragraph_format.space_after = Pt(2)

                self._p("【题目】", size=10.5, bold=True, indent=0.15)
                body = self._c(ex.statement_latex or ex.statement_plain)
                for line in body.split("\n"):
                    if line.strip():
                        self._p(line.strip(), indent=0.2)
                self._image(ex.shot, "原视频画面")

                if ex.answer:
                    self._p("【答案】", size=10.5, bold=True, indent=0.15)
                    self._p(self._c(ex.answer), indent=0.2)

                steps = [self._c(s) for s in ex.solution if self._c(s)]
                if steps:
                    self._p("【解析】", size=10.5, bold=True, indent=0.15)
                    for i, step in enumerate(steps, 1):
                        para = self.doc.add_paragraph()
                        para.paragraph_format.left_indent = Inches(0.35)
                        para.paragraph_format.space_after = Pt(2)
                        run = para.add_run(f"{i}. ")
                        run.font.bold = True
                        run.font.name = LATIN_FONT
                        self.add_rich(para, step)
                else:
                    self._p("▸ 视频里老师只报了答案，没有展开讲解。",
                            size=9, color=RGBColor(0x88, 0x88, 0x88), indent=0.2)

                if ex.notes:
                    self._p(f"▸ 提示：{self._c(ex.notes)}", size=9,
                            color=RGBColor(0x88, 0x88, 0x88), indent=0.2)

    def _common_mistakes(self) -> None:
        if not self.lesson.common_mistakes:
            return
        self._p("五、易错点汇总", style="Heading 1")
        for item in self.lesson.common_mistakes:
            self._bullet(self._c(item))

    def _flow_table(self) -> None:
        if not self.lesson.flow:
            return
        self._p("七、课堂流程与时间分配", style="Heading 1")
        table = self.doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        headers = ["环节", "时长(分)", "教师活动", "教师话术", "设计意图"]
        for i, text in enumerate(headers):
            _set_cell(table.rows[0].cells[i], text, bold=True, shade="EEF3FA")
        for step in self.lesson.flow:
            cells = table.add_row().cells
            _set_cell(cells[0], self._c(step.stage))
            _set_cell(cells[1], f"{step.duration_min:.0f}")
            _set_cell(cells[2], self._c(step.activity))
            _set_cell(cells[3], self._c(step.teacher_script))
            _set_cell(cells[4], self._c(step.intent))
        for row in table.rows:
            row.cells[0].width = Inches(0.9)
            row.cells[1].width = Inches(0.6)
            row.cells[2].width = Inches(1.7)
            row.cells[3].width = Inches(1.8)
            row.cells[4].width = Inches(1.4)

    def _homework(self) -> None:
        if not self.lesson.homework:
            return
        self._p("八、课后作业", style="Heading 1")
        self._homework_items()

    def _homework_items(self) -> None:
        for i, item in enumerate(self.lesson.homework, 1):
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Inches(0.28)
            run = para.add_run(f"{i}. ")
            run.font.bold = True
            run.font.name = LATIN_FONT
            self.add_rich(para, self._c(item))

    def _review(self) -> None:
        r = self.lesson.review
        self._p("附、审核记录", style="Heading 1")
        # 模型交叉审核没跑成时，不能拿"规则体检通过"冒充"审核通过"
        if r.model_review_ok:
            verdict_cn = {"pass": "✅ 通过", "revise": "⚠ 已修订，仍建议人工复核", "fail": "❌ 未通过"}.get(
                r.verdict, r.verdict
            )
            self._p(f"审核结论：{verdict_cn}　|　得分：{r.score:.0f}　|　回炉轮数：{r.rounds}")
        else:
            self._p(
                "审核结论：⚠ 模型交叉审核未完成，仅通过本地规则体检",
                bold=True,
            )
            self._p(
                f"本地规则体检：{'通过' if r.verdict == 'pass' else '未通过'}"
                f"　|　模型得分：未产生　|　回炉轮数：{r.rounds}",
                size=10,
            )
        if r.hallucination_risk:
            self._p(f"编造风险评估：{r.hallucination_risk}")
        if r.summary:
            self._p(f"审核意见：{r.summary}")
        if r.rule_checks:
            self._p("规则体检：", size=10.5, bold=True)
            for item in r.rule_checks:
                self._bullet(item)
        if r.issues:
            # 事实错误和教学补充分开列。混在一起的话，老师会以为口诀、作业
            # 也是错误，白紧张一场——这两类的性质根本不同。
            facts = [i for i in r.issues if i.kind != "supplement"]
            supps = [i for i in r.issues if i.kind == "supplement"]

            def _issue_block(issue, mark: str) -> None:
                para = self.doc.add_paragraph()
                para.paragraph_format.left_indent = Inches(0.28)
                run = para.add_run(f"{mark} [{issue.target or '整体'}] {issue.problem}")
                run.font.size = Pt(9.5)
                run.font.name = LATIN_FONT
                run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
                if issue.suggestion:
                    sub = self.doc.add_paragraph()
                    sub.paragraph_format.left_indent = Inches(0.5)
                    run2 = sub.add_run(f"→ {issue.suggestion}")
                    run2.font.size = Pt(9)
                    run2.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
                    run2.font.name = LATIN_FONT
                    run2._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

            if facts:
                self._p(f"需要核对的地方（{len(facts)} 条）：", size=10.5, bold=True)
                for issue in facts:
                    _issue_block(issue, {"blocker": "🔴", "major": "🟠", "minor": "🟡"}[issue.severity])
            if supps:
                self._p(
                    f"补充建议（{len(supps)} 条，非错误，视频里没讲但老师通常会自己补）：",
                    size=10.5, bold=True,
                )
                for issue in supps:
                    _issue_block(issue, "📝")
        if r.teacher_notes:
            self._p(f"▸ 审核老师提醒：{r.teacher_notes}")

    def _footer_note(self) -> None:
        meta = self.lesson.meta
        self.doc.add_paragraph()
        note = self._p(
            f"本教案由 {meta.generator} 从视频《{meta.source_title}》自动生成，"
            f"知识点与例题均可回溯到原视频时间戳（{meta.source_url}）。"
            "公式为 Word 原生公式对象，可直接编辑；请在上课前按学生实际情况微调。",
            size=8.5, color=RGBColor(0x99, 0x99, 0x99),
        )
        note.alignment = WD_ALIGN_PARAGRAPH.LEFT


def _set_cell(cell, text: str, bold: bool = False, shade: str | None = None) -> None:
    cell.text = ""
    para = cell.paragraphs[0]
    para.paragraph_format.space_after = Pt(1)
    para.paragraph_format.space_before = Pt(1)
    run = para.add_run(text)
    run.font.size = Pt(9.5)
    run.font.bold = bold
    run.font.name = LATIN_FONT
    run._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    if shade:
        tc_pr = cell._tc.get_or_add_tcPr()
        shd = parse_xml(
            f'<w:shd xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            f'w:val="clear" w:color="auto" w:fill="{shade}"/>'
        )
        tc_pr.append(shd)


def build_docx(lesson: Lesson, shots_dir: Path, out_path: Path, work_dir: Path,
               audience: str = "teacher") -> Path:
    return DocxBuilder(lesson, shots_dir, work_dir, audience=audience).build(out_path)


def build_both(lesson: Lesson, shots_dir: Path, work_dir: Path,
               teacher_name: str, student_name: str) -> tuple[Path, Path]:
    """一份数据出两份 Word：教师备课版（详）+ 学生讲义版（简）。

    学生版是教师版的降级投影，不重新调用模型——所以两份内容永远一致，
    不会出现"学生版讲的和老师手上的对不上"。
    """
    teacher = build_docx(lesson, shots_dir, work_dir / teacher_name, work_dir, audience="teacher")
    student = build_docx(lesson, shots_dir, work_dir / student_name, work_dir, audience="student")
    return teacher, student