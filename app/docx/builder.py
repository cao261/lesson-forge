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

# ---- 图片排版 ----
# 所有题图/板书图共用同一个尺寸框，版面才齐（见 _fit_box）。
IMG_MAX_W = 5.5
IMG_MAX_H = 3.0
# 放大上限（PPI）。截图都出自同一帧视频，按固定 PPI 折算，图里的字在
# 题与题之间就是同一个物理大小；不给上限的话低清小裁切会被拉糊。
MAX_PPI = 110.0

MATH_PARA_HEAD = '<m:oMathPara xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><m:oMathParaPr><m:jc m:val="center"/></m:oMathParaPr>'


class DocxBuilder:
    """audience 决定这份文档给谁看：

    - ``teacher`` 教师备课版：知识点讲全、解析分步、附讲法提示与易错点、末尾附审核记录。
    - ``student`` 学生版：只有粗略知识点和题目（题干原图 + 答案），
      任何"讲法提示""易错提醒""审核记录"都不出现——那些是老师自己看的，
      端给学生只会让他分心。
    """

    def __init__(self, lesson: Lesson, shots_dir: Path, work_dir: Path,
                 audience: str = "teacher", owner: str = "") -> None:
        self.lesson = lesson
        self.shots_dir = shots_dir
        self.audience = audience
        # owner = 产出这份文档的任务名，写进核心属性。成品目录是跨任务共用的，
        # 靠它判断"这份是不是同一个任务产的"，决定能不能覆盖同名文件。
        self.owner = owner
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

    def _fit_box(self, path: Path, max_width: float, max_height: float) -> tuple[float, float]:
        """把图片等比缩放进统一尺寸的框，返回 (宽英寸, 高英寸)。

        两条规则取更严的：
        1. 等比内缩进 max_width × max_height —— 所有图占的版面一样，版面才齐。
        2. 放大上限 MAX_PPI —— 截图都来自同一帧视频，按固定 PPI 折算，
           画面里的字在题与题之间就是同一个物理大小；没有这条的话，
           一张低清小裁切会被拉到半页宽，糊得看不清。

        取不到尺寸就退回按宽度插图，行为和以前一致。
        """
        try:
            from PIL import Image

            with Image.open(path) as im:
                w_px, h_px = im.size
        except Exception:  # noqa: BLE001
            return max_width, 0.0
        if not w_px or not h_px:
            return max_width, 0.0

        scale = min(max_width / w_px, max_height / h_px, 1.0 / MAX_PPI)
        return round(w_px * scale, 2), round(h_px * scale, 2)

    def _image(self, filename: str | None, caption: str = "", max_width: float = IMG_MAX_W,
               max_height: float = IMG_MAX_H):
        if not filename:
            return
        path = self.shots_dir / filename
        # 优先使用清晰/纯净裁剪版截图
        for candidate_name in (
            path.stem + "_clean.png",
            path.stem + "_clean.jpg",
            path.stem.split("_")[0] + "_clean.png",
            path.stem.split("_")[0] + "_clean.jpg",
        ):
            candidate = self.shots_dir / candidate_name
            if candidate.exists():
                path = candidate
                break
        if not path.exists():
            return
        try:
            # 统一图片框 + 等比内缩。以前是按宽高比分三档给宽度、**不管高度**，
            # 1188×295 的横条和 650×557 的方图贴出来一大一小，版面很不齐。
            # 现在所有图都等比缩放进同一个框，版面才统一。
            #
            # 再加一条放大上限：截图源自同一帧视频，按固定 PPI 折算，
            # 画面里的字在题与题之间就是同一个物理大小；不给上限的话，
            # 一张 300px 的小裁切会被拉到 4.5 英寸宽，糊得看不清。
            target_width, target_height = self._fit_box(path, max_width, max_height)

            para = self.doc.add_paragraph()
            para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            para.paragraph_format.space_before = Pt(4)
            para.paragraph_format.space_after = Pt(2)
            run = para.add_run()
            if target_height:
                run.add_picture(str(path), width=Inches(target_width), height=Inches(target_height))
            else:
                run.add_picture(str(path), width=Inches(target_width))
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
            # 独立公式：oMathPara 必须包裹完整的 oMath，符合 ECMA-376 规范且 Word 桌面端不报损坏
            para._p.append(parse_xml(MATH_PARA_HEAD + omml + "</m:oMathPara>"))
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
            self._p("一、课堂同步精讲与针对演练", style="Heading 1")
            self._modules_student()
            if L.homework:
                self._p("二、课后巩固拓展", style="Heading 1")
                self._homework_student()
            self._p("三、参考答案与分步解析", style="Heading 1")
            self._answers_student()
        else:
            self._info_table()
            CN_NUMS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
            sec_idx = 0

            if self.lesson.objectives:
                self._objectives(CN_NUMS[sec_idx])
                sec_idx += 1
            if self.lesson.key_points or self.lesson.difficulties:
                self._key_difficulties(CN_NUMS[sec_idx])
                sec_idx += 1
            self._p(f"{CN_NUMS[sec_idx]}、核心知识精讲与针对典例剖析（讲练一体）", style="Heading 1")
            sec_idx += 1
            self._modules_teacher()
            if self.lesson.common_mistakes:
                self._common_mistakes(CN_NUMS[sec_idx])
                sec_idx += 1
            if self.lesson.materials:
                self._materials(CN_NUMS[sec_idx])
                sec_idx += 1
            if self.lesson.flow:
                self._flow_table(CN_NUMS[sec_idx])
                sec_idx += 1
            if self.lesson.homework:
                self._homework(CN_NUMS[sec_idx])
                sec_idx += 1
            self._review()
            self._footer_note()

        out_path.parent.mkdir(parents=True, exist_ok=True)
        # 归属任务名必须在这次 save 里一次性写进去。绝不要"存完再打开改一次属性存回去"，
        # 那样等于让 python-docx 重新序列化一遍包里的 XML，含图片的文档会被它弄坏命名空间。
        if self.owner:
            self.doc.core_properties.category = self.owner
        self.doc.save(str(out_path))
        return out_path

    # ------------------------------------------------------------------ 学生版（讲练结合）
    def _student_head(self) -> None:
        """学生版顶部提示：讲练结合，以做题演练为主。"""
        meta = self.lesson.meta
        bits = [b for b in (meta.subject, meta.grade) if b]
        head = self._p(align=WD_ALIGN_PARAGRAPH.CENTER, size=10, color=RGBColor(0x55, 0x55, 0x55))
        head.add_run("　".join(bits) + "　·　课堂同步讲义与练习 · 先独立演练，课后核对答案")
        head.paragraph_format.space_after = Pt(12)

    def _add_practice_box(self, lines: int = 5) -> None:
        """为学生版留出舒适的解题草稿与书写区域。"""
        p = self._p("【解题与演练区】：", size=9.5, color=RGBColor(0x88, 0x88, 0x88), indent=0.2)
        p.paragraph_format.space_before = Pt(4)
        p.paragraph_format.space_after = Pt(2)
        tbl = self.doc.add_table(rows=1, cols=1)
        tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
        cell = tbl.rows[0].cells[0]
        cell.width = Inches(5.8)
        # 留白高度
        tc_p = cell.paragraphs[0]
        tc_p.paragraph_format.space_before = Pt(lines * 8)
        tc_p.paragraph_format.space_after = Pt(lines * 8)
        run = tc_p.add_run("（请在虚线框内书写解题思路与计算步骤）")
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor(0xBB, 0xBB, 0xBB)
        # 只铺底纹。不能走 _set_cell：它第一行就是 cell.text = ""，
        # 会把上面刚加的提示文字和 lines*8 的留白高度一起清成 1pt，
        # 学生讲义的书写区塌成一条发丝线，提示也没了。
        tc_pr = cell._tc.get_or_add_tcPr()
        tc_pr.append(parse_xml(
            f'<w:shd xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
            f'w:val="clear" w:color="auto" w:fill="FAFAFA"/>'
        ))
        self.doc.add_paragraph().paragraph_format.space_after = Pt(4)

    def _modules_student(self) -> None:
        """学生版讲练结合：按知识点编排，每个知识点后紧跟对应练习题目。"""
        current_ch = ""
        kp_in_ch = 0
        for idx, kp in enumerate(self.lesson.knowledge, 1):
            m = re.match(r"^【第(\d+)讲\s*[·・-]?\s*(.*?)】(.*)$", kp.title)
            if m:
                ch_num, raw_ch_name, kp_name = m.group(1), m.group(2).strip(), m.group(3).strip()
                ch_name = re.sub(r"^(讲|直线[和与]圆[·・-]?)", "", raw_ch_name).strip()
                if ch_num != current_ch:
                    current_ch = ch_num
                    kp_in_ch = 0
                    ch_p = self._p(f"第 {ch_num} 讲 · {ch_name}", style="Heading 2")
                    ch_p.paragraph_format.space_before = Pt(14)
                    ch_p.paragraph_format.space_after = Pt(6)
                kp_in_ch += 1
                kp_heading = f"考点 {kp_in_ch} · {kp_name}"
                head_style = "Heading 3"
            else:
                kp_heading = f"考点 {idx} · {self._c(kp.title)}"
                head_style = "Heading 2"

            self._p(kp_heading, style=head_style)

            # 1. 精炼知识点速记（学生版只看核心结论）
            self._p("【知识要点速记】", size=10, bold=True, color=RGBColor(0x2E, 0x5C, 0x8A), indent=0.1)
            definition = self._c(kp.definition)
            if definition:
                self._p(definition, indent=0.2)

            if kp.formula_latex:
                math_para = self.doc.add_paragraph()
                math_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                self._add_math(math_para, self._c(kp.formula_latex), block=True)

            for item in kp.key_points:
                self._bullet(self._c(item), level=0)

            if not kp.key_points and not definition and not kp.formula_latex and kp.summary:
                self._p(self._c(kp.summary), indent=0.2)

            # 学生版讲义绝不放老师手写演算的板书截图，确保学生端毫无解题痕迹
            # 2. 配套针对精练（不直接给答案，留出做题空间）
            if kp.examples:
                self._p("【即学即练·针对精练】", size=10.5, bold=True, color=RGBColor(0x1F, 0x4E, 0x79), indent=0.1)
                for ex in kp.examples:
                    self._p(f"{ex.id}．{self._c(ex.title)}", size=11, bold=True, indent=0.15)
                    for line in (self._c(ex.statement_latex or ex.statement_plain)).split("\n"):
                        if line.strip():
                            self._p(line.strip(), indent=0.2)
                    self._image(ex.shot, f"{ex.id} 题目图")
                    self._add_practice_box(lines=5)

    def _homework_student(self) -> None:
        """学生版课后作业。"""
        for i, item in enumerate(self.lesson.homework, 1):
            para = self.doc.add_paragraph()
            para.paragraph_format.left_indent = Inches(0.2)
            run = para.add_run(f"习题 {i}. ")
            run.font.bold = True
            run.font.name = LATIN_FONT
            self.add_rich(para, self._c(item))
            self._add_practice_box(lines=4)

    def _answers_student(self) -> None:
        """学生版参考答案（放在文末，课下对照）。"""
        self._p("【课堂精练参考答案】", size=11, bold=True, color=RGBColor(0x1F, 0x4E, 0x79))
        current_ch = ""
        for kp in self.lesson.knowledge:
            m = re.match(r"^【第(\d+)讲\s*[·・-]?\s*(.*?)】(.*)$", kp.title)
            if m and kp.examples:
                ch_num, raw_ch_name = m.group(1), m.group(2).strip()
                ch_name = re.sub(r"^(讲|直线[和与]圆[·・-]?)", "", raw_ch_name).strip()
                if ch_num != current_ch:
                    current_ch = ch_num
                    ans_ch = self._p(f"第 {ch_num} 讲 · {ch_name}", size=11, bold=True, color=RGBColor(0x1F, 0x38, 0x64))
                    ans_ch.paragraph_format.space_before = Pt(8)
                    ans_ch.paragraph_format.space_after = Pt(2)
            for ex in kp.examples:
                if ex.answer or ex.solution:
                    ans_p = self.doc.add_paragraph()
                    ans_p.paragraph_format.left_indent = Inches(0.2)
                    ans_p.paragraph_format.space_after = Pt(2)
                    run = ans_p.add_run(f"{ex.id}（{self._c(ex.title)}）：")
                    run.font.bold = True
                    if ex.answer:
                        ans_label = ans_p.add_run("答案：")
                        ans_label.font.bold = True
                        self.add_rich(ans_p, self._c(ex.answer))
                    steps = [self._c(s) for s in ex.solution if self._c(s)]
                    if steps:
                        for si, step in enumerate(steps, 1):
                            sp = self.doc.add_paragraph()
                            sp.paragraph_format.left_indent = Inches(0.4)
                            sp.paragraph_format.space_after = Pt(1)
                            r = sp.add_run(f"步骤 {si}：")
                            r.font.size = Pt(9.5)
                            self.add_rich(sp, step, size=9.5)


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

    def _objectives(self, num: str = "一") -> None:
        if not self.lesson.objectives:
            return
        self._p(f"{num}、教学目标", style="Heading 1")
        for item in self.lesson.objectives:
            self._bullet(self._c(item))

    def _key_difficulties(self, num: str = "二") -> None:
        self._p(f"{num}、教学重点与难点", style="Heading 1")
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

    def _materials(self, num: str = "五") -> None:
        if not self.lesson.materials:
            return
        self._p(f"{num}、教学准备与教具", style="Heading 1")
        for item in self.lesson.materials:
            self._bullet(self._c(item))

    def _modules_teacher(self) -> None:
        """教师版讲练结合：按知识点编排，每个知识点后紧跟其对应的针对性例题剖析。"""
        current_ch = ""
        kp_in_ch = 0
        for idx, kp in enumerate(self.lesson.knowledge, 1):
            m = re.match(r"^【第(\d+)讲\s*[·・-]?\s*(.*?)】(.*)$", kp.title)
            if m:
                ch_num, raw_ch_name, kp_name = m.group(1), m.group(2).strip(), m.group(3).strip()
                ch_name = re.sub(r"^(讲|直线[和与]圆[·・-]?)", "", raw_ch_name).strip()
                if ch_num != current_ch:
                    current_ch = ch_num
                    kp_in_ch = 0
                    ch_p = self._p(f"第 {ch_num} 讲 · {ch_name}", style="Heading 2")
                    ch_p.paragraph_format.space_before = Pt(14)
                    ch_p.paragraph_format.space_after = Pt(6)
                kp_in_ch += 1
                kp_heading = f"考点 {kp_in_ch} · {kp_name}"
                head_style = "Heading 3"
            else:
                kp_heading = f"考点 {idx} · {self._c(kp.title)}"
                head_style = "Heading 2"

            self._p(kp_heading, style=head_style)

            # 1. 核心概念与结论
            if kp.definition:
                p = self._p(indent=0.15)
                r = p.add_run("【核心概念与定理】 ")
                r.bold = True
                r.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
                self.add_rich(p, self._c(kp.definition))
            if kp.summary and kp.summary != kp.definition:
                p = self._p(indent=0.15)
                r = p.add_run("【内容精要与内涵阐释】 ")
                r.bold = True
                r.font.color.rgb = RGBColor(0x2E, 0x5C, 0x8A)
                self.add_rich(p, self._c(kp.summary))

            # 2. 核心公式
            if kp.formula_latex:
                p = self._p("【核心公式与推导结论】", size=10.5, bold=True, color=RGBColor(0x1F, 0x4E, 0x79), indent=0.15)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(2)
                math_para = self.doc.add_paragraph()
                math_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
                self._add_math(math_para, self._c(kp.formula_latex), block=True)

            # 3. 教学要点
            if kp.key_points:
                p = self._p("【教学重点与考向梳理】", size=10.5, bold=True, color=RGBColor(0x1F, 0x4E, 0x79), indent=0.15)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(2)
                for item in kp.key_points:
                    self._bullet(self._c(item))

            # 4. 易错警示
            if kp.pitfalls:
                p = self._p("【易错警示与避坑指南】", size=10.5, bold=True, color=RGBColor(0xC0, 0x39, 0x2B), indent=0.15)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(2)
                for item in kp.pitfalls:
                    self._bullet(self._c(item))

            # 5. 备课与讲法点拨
            if kp.teaching_notes:
                p = self._p("【备课点拨与授课启发】", size=10, bold=True, color=RGBColor(0xB0, 0x60, 0x00), indent=0.15)
                p.paragraph_format.space_before = Pt(3)
                p.paragraph_format.space_after = Pt(2)
                note = self._p(size=10.5, indent=0.15)
                head = note.add_run("▸ 授课启发与引导：")
                head.font.bold = True
                head.font.color.rgb = RGBColor(0xB0, 0x60, 0x00)
                head.font.name = LATIN_FONT
                head._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
                self.add_rich(note, self._c(kp.teaching_notes))

            # 6. 板书画面
            if kp.board_shot:
                self._image(kp.board_shot, "板书精要总结画面")

            # 7. 紧跟针对性典型例题！
            if kp.examples:
                sub_head = self._p("【配套针对典例精讲】", size=11, bold=True, color=RGBColor(0x1F, 0x38, 0x64), indent=0.1)
                sub_head.paragraph_format.space_before = Pt(6)
                sub_head.paragraph_format.space_after = Pt(3)
                for ex in kp.examples:
                    self._render_example_teacher(ex)

    def _render_example_teacher(self, ex: Example) -> None:
        """教师版典例精析：排版紧凑美观、讲练针对性强。"""
        # 1. 例题头部标题
        head_p = self._p(style="Heading 3")
        r = head_p.add_run(f"▸ 针对典例 · {ex.id}．{self._c(ex.title)}")
        r.bold = True

        # 2. 标签与难度信息
        bits = []
        if ex.difficulty:
            bits.append(f"难度：{ex.difficulty}")
        if ex.tags:
            bits.append(f"考点分类：{'、'.join(ex.tags)}")
        if bits:
            tag_p = self._p(size=9, color=RGBColor(0x66, 0x66, 0x66), indent=0.15)
            tag_p.add_run("〔" + "　｜　".join(bits) + "〕")
            tag_p.paragraph_format.space_after = Pt(2)

        # 3. 题干与原图
        stmt = self._c(ex.statement_latex or ex.statement_plain)
        if stmt:
            p_q = self._p(indent=0.15)
            p_q.add_run("【题面与已知条件】 ").bold = True
            for line in stmt.split("\n"):
                line_str = line.strip()
                if line_str:
                    self._p(line_str, indent=0.25)
        self._image(ex.shot, f"{ex.id} 题目原图")
        if not ex.shot and not ex.verified:
            # 没贴图不是因为漏了，是多模态确认过这几帧里根本没有题目。
            # 什么都不印的话老师根本看不出这里缺东西，得明说。
            hint = self._p(indent=0.15)
            hr = hint.add_run(f"［{ex.id}］视频画面里没有这道题的题图，请老师自行截图补上")
            hr.font.size = Pt(8.5)
            hr.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
            hr.font.name = LATIN_FONT
            hr._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
            hint.paragraph_format.space_after = Pt(2)

        # 4. 参考答案
        if ex.answer:
            ans_p = self._p(indent=0.15)
            r_ans = ans_p.add_run("【参考答案】 ")
            r_ans.bold = True
            r_ans.font.color.rgb = RGBColor(0x1F, 0x4E, 0x79)
            self.add_rich(ans_p, self._c(ex.answer))

        # 5. 规范分步精析
        steps = [self._c(s) for s in ex.solution if self._c(s)]
        if steps:
            p_sol = self._p("【规范解法与分步剖析】", size=10, bold=True, color=RGBColor(0x1F, 0x4E, 0x79), indent=0.15)
            p_sol.paragraph_format.space_before = Pt(3)
            p_sol.paragraph_format.space_after = Pt(2)
            for i, step in enumerate(steps, 1):
                para = self.doc.add_paragraph()
                para.paragraph_format.left_indent = Inches(0.35)
                para.paragraph_format.space_after = Pt(2)
                run = para.add_run(f"第 {i} 步：")
                run.font.bold = True
                run.font.color.rgb = RGBColor(0x2E, 0x5C, 0x8A)
                run.font.name = LATIN_FONT
                run._element.rPr.rFonts.set(qn("w:eastAsia"), HEAD_FONT)
                self.add_rich(para, step)
        else:
            self._p("▸ 提示：视频中主要为教师口头点拨与引导分析，建议引导学生在草稿本上独立推导演算。",
                    size=9, color=RGBColor(0x88, 0x88, 0x88), indent=0.2)

        # 6. 名师点拨与解题通法
        if ex.notes:
            note_p = self._p(indent=0.15)
            note_p.paragraph_format.space_before = Pt(2)
            h = note_p.add_run("【名师点拨与解题通法】 ")
            h.bold = True
            h.font.color.rgb = RGBColor(0xB0, 0x60, 0x00)
            self.add_rich(note_p, self._c(ex.notes), size=9.5)

    def _common_mistakes(self, num: str = "四") -> None:
        if not self.lesson.common_mistakes:
            return
        self._p(f"{num}、全课核心思想与易错点汇总", style="Heading 1")
        for item in self.lesson.common_mistakes:
            self._bullet(self._c(item))

    def _flow_table(self, num: str = "六") -> None:
        if not self.lesson.flow:
            return
        self._p(f"{num}、课堂流程与时间分配建议", style="Heading 1")
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

    def _homework(self, num: str = "七") -> None:
        if not self.lesson.homework:
            return
        self._p(f"{num}、课后作业与巩固练习", style="Heading 1")
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
        self._p("附、教案审核与备课说明", style="Heading 1")
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
               audience: str = "teacher", owner: str = "") -> Path:
    return DocxBuilder(lesson, shots_dir, work_dir, audience=audience, owner=owner).build(out_path)


def build_both(lesson: Lesson, shots_dir: Path, work_dir: Path,
               teacher_name: str, student_name: str) -> tuple[Path, Path]:
    """一份数据出两份 Word：教师备课版（详）+ 学生讲义版（简）。

    学生版是教师版的降级投影，不重新调用模型——所以两份内容永远一致，
    不会出现"学生版讲的和老师手上的对不上"。
    """
    teacher = build_docx(lesson, shots_dir, work_dir / teacher_name, work_dir, audience="teacher")
    student = build_docx(lesson, shots_dir, work_dir / student_name, work_dir, audience="student")
    return teacher, student