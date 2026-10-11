"""阶段 8：导出。Word 是最终交付物，HTML 是浏览器里的快速预览。"""
from __future__ import annotations

import html
import re
from pathlib import Path

from app.docx.builder import build_docx
from app.docx.sanitize import clean_block
from app.models import Lesson, fmt_ts

MATHJAX_LOCAL = "/static/mathjax/tex-chtml.js"
MATHJAX_CDN = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"

# 本地 MathJax（已随项目内置，离线也能渲染公式）；本地拿不到时自动回退到 CDN
MATHJAX_TAG = (
    f'<script src="{MATHJAX_LOCAL}" onerror="this.onerror=null;'
    f"var s=document.createElement('script');s.src='{MATHJAX_CDN}';document.head.appendChild(s);\"></script>"
)


# ------------------------------------------------------------------ Word
def export_docx_both(lesson: Lesson, shots_dir: Path, work_dir: Path) -> tuple[Path, Path]:
    """一次导出两份：教师备课版 + 学生讲义版。

    两份出自同一份 Lesson，学生版就是教师版的降级投影——不额外调模型，
    所以绝不会出现"学生手上的和老师讲的对不上"。
    """
    safe = _safe_name(lesson.meta.title or "教案")
    teacher = build_docx(lesson, shots_dir, work_dir / f"{safe}·教师备课版.docx",
                         work_dir, audience="teacher")
    student = build_docx(lesson, shots_dir, work_dir / f"{safe}·学生讲义.docx",
                         work_dir, audience="student")
    return teacher, student


# 兼容旧调用点
def export_docx(lesson: Lesson, shots_dir: Path, work_dir: Path) -> Path:
    return export_docx_both(lesson, shots_dir, work_dir)[0]


# ------------------------------------------------------------------ HTML 预览
CSS = """
:root { --ink:#1c1c1e; --muted:#6b7280; --line:#e5e7eb; --accent:#1f4e79; --warn:#c0392b; }
* { box-sizing: border-box; }
body { margin:0; padding:28px 18px 80px; background:#f6f7f9; color:var(--ink);
  font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif; line-height:1.75; }
.page { max-width: 880px; margin:0 auto; background:#fff; padding:44px 52px;
  border-radius:14px; box-shadow:0 2px 18px rgba(0,0,0,.06); }
h1 { font-size:26px; text-align:center; margin:0 0 6px; color:var(--accent); }
.sub { text-align:center; color:var(--muted); font-size:12px; margin-bottom:22px; }
h2 { font-size:19px; margin:32px 0 12px; padding-bottom:6px; border-bottom:2px solid var(--line); color:var(--accent); }
h3 { font-size:16px; margin:22px 0 8px; color:#2e5c8a; }
h4 { font-size:14.5px; margin:18px 0 6px; color:#374151; }
.meta { width:100%; border-collapse:collapse; margin:14px 0 8px; font-size:13px; }
.meta td { border:1px solid var(--line); padding:6px 10px; }
.meta td.k { background:#eef3fa; width:88px; font-weight:600; color:#334155; }
ul { margin:6px 0 10px; padding-left:22px; }
li { margin:3px 0; }
.ts { color:var(--muted); font-size:12px; font-weight:400; }
.note { background:#f8fafc; border-left:3px solid var(--accent); padding:8px 12px; margin:8px 0; font-size:13.5px; }
.warn { background:#fdf1f0; border-left:3px solid var(--warn); color:var(--warn); padding:8px 12px; margin:8px 0; font-size:13px; }
.quote { color:var(--muted); font-size:12px; font-style:italic; margin:4px 0 10px; }
.shot { display:block; margin:12px auto; max-width:100%; border:1px solid var(--line); border-radius:8px; }
.cap { text-align:center; color:var(--muted); font-size:11.5px; margin:-4px 0 12px; }
table.grid { width:100%; border-collapse:collapse; font-size:13px; margin:10px 0; }
table.grid th, table.grid td { border:1px solid var(--line); padding:6px 9px; vertical-align:top; text-align:left; }
table.grid th { background:#eef3fa; font-weight:600; }
.marker { background:#fffbe6; }
.verdict.pass { color:#15803d; font-weight:600; }
.verdict.revise { color:#b45309; font-weight:600; }
.verdict.fail { color:var(--warn); font-weight:600; }
"""


def export_html(lesson: Lesson, shots_dir: Path, out_path: Path, audience: str = "teacher") -> Path:
    body = _html_body(lesson, shots_dir, audience=audience)
    label = "配套讲义" if audience == "student" else "教师备课版"
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(lesson.meta.title or '教案')}</title>
<style>{CSS}</style>
<script>
window.MathJax = {{
  tex: {{
    inlineMath: [['$', '$'], ['\\\\(', '\\\\)']],
    displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']],
    processEscapes: true,
    processEnvironments: true
  }},
  options: {{
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }},
  chtml: {{ scale: 1.0 }}
}};
</script>
{MATHJAX_TAG}
</head><body><div class="page">{body}
<div style="margin-top:34px;padding-top:14px;border-top:1px solid var(--line);color:#9aa1ac;font-size:12px">
{label} · 由 {_e(lesson.meta.generator)} 依据教学视频自动生成 · {html.escape(lesson.meta.generated_at)}
</div></div></body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")
    return out_path


def _bullets(items: list[str]) -> str:
    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def _img(shots_dir: Path, filename: str | None, caption: str) -> str:
    if not filename or not (shots_dir / filename).exists():
        return ""
    return (
        f'<img class="shot" src="shots/{html.escape(filename)}" alt="{html.escape(caption)}">'
        f'<div class="cap">{html.escape(caption)}</div>'
    )


from app.docx.omml import clean_latex, split_math

def _e(text: str) -> str:
    """转义前先清洗，保证预览里也不会冒出 think/围栏/JSON 残留。"""
    return html.escape(clean_block(text or ""))


def _math_html(text: str) -> str:
    """安全地将混排文本转为 HTML：文本转义，数学公式保持纯净 LaTeX 供 MathJax 渲染。"""
    if not text:
        return ""
    text = clean_block(text)
    parts: list[str] = []
    for kind, body, _ in split_math(text):
        if kind == "text":
            parts.append(html.escape(body))
        elif kind == "inline":
            clean_b = clean_latex(body)
            parts.append(f"${clean_b}$")
        elif kind == "block":
            clean_b = clean_latex(body)
            parts.append(f"$${clean_b}$$")
    return "".join(parts)


def _html_body(L: Lesson, shots_dir: Path, audience: str = "teacher") -> str:
    if audience == "student":
        return _html_student(L, shots_dir)
    m = L.meta
    r = L.review
    verdict = {"pass": "✅ 通过", "revise": "⚠ 已修订，建议人工复核", "fail": "❌ 未通过"}.get(r.verdict, r.verdict)
    parts: list[str] = []

    parts.append(f"<h1>{_e(m.title or '教案')}</h1>")
    parts.append('<div class="sub">教师备课版 · 教学设计 / 知识精讲 / 典型例题精析</div>')

    meta_rows = [
        ("学科", m.subject), ("年级", m.grade), ("建议课时", f"{m.duration_min:.0f} 分钟"),
        ("授课教师", m.teacher or "＿＿＿＿"), ("学生", m.student or "＿＿＿＿"),
        ("视频来源", m.source_title), ("UP 主", m.uploader),
        ("视频时长", fmt_ts(m.video_duration)),
        ("审核结论", f"{verdict}（{r.rounds} 轮回炉）"),
    ]
    parts.append(
        '<table class="meta">'
        + "".join(f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>' for k, v in meta_rows)
        + "</table>"
    )

    if L.objectives:
        parts.append("<h2>一、教学目标与重难点</h2>")
        parts.append("<h4>教学目标</h4>" + _bullets([_math_html(x) for x in L.objectives]))
        if L.key_points:
            parts.append("<h4>教学重点</h4>" + _bullets([_math_html(x) for x in L.key_points]))
        if L.difficulties:
            parts.append("<h4>教学难点</h4>" + _bullets([_math_html(x) for x in L.difficulties]))
        if L.prerequisites:
            parts.append("<h4>前置知识</h4>" + _bullets([_math_html(x) for x in L.prerequisites]))

    parts.append("<h2>二、核心知识精讲与备课指南</h2>")
    for idx, kp in enumerate(L.knowledge, 1):
        parts.append(f'<h3>{idx}. {_math_html(kp.title)}</h3>')
        if kp.definition:
            parts.append(f'<div class="note"><b>【核心概念/结论】</b> {_math_html(kp.definition)}</div>')
        elif kp.summary:
            parts.append(f'<div class="note"><b>【知识精要】</b> {_math_html(kp.summary)}</div>')
        if kp.formula_latex:
            pure_f = clean_latex(kp.formula_latex)
            parts.append(f'<p><b>【核心公式】</b></p><div style="margin:10px 0;text-align:center">$${pure_f}$$</div>')
        if kp.key_points:
            parts.append("<h4>教学要点</h4>" + _bullets([_math_html(x) for x in kp.key_points]))
        if kp.pitfalls:
            parts.append("<h4>易错警示</h4>" + _bullets([_math_html(x) for x in kp.pitfalls]))
        if kp.teaching_notes:
            parts.append(f'<div class="note" style="border-left-color:#b06000;background:#fffaf0"><b>▸ 授课启发与引导：</b>{_math_html(kp.teaching_notes)}</div>')
        if kp.board_shot:
            parts.append(_img(shots_dir, kp.board_shot, "板书总结画面"))

    if L.example_count():
        parts.append("<h2>三、典型例题精析</h2>")
        for kp in L.knowledge:
            for ex in kp.examples:
                bits = "　".join(filter(None, [
                    f"难度：{ex.difficulty}" if ex.difficulty else "",
                    f"考点：{'、'.join(ex.tags)}" if ex.tags else "",
                ]))
                parts.append(f'<h3>{_e(ex.id)}．{_math_html(ex.title)} <span class="ts">{_e(bits)}</span></h3>')
                if not ex.verified:
                    parts.append('<div class="warn">⚠ 该题截图建议人工复核。</div>')
                body_text = ex.statement_latex or ex.statement_plain
                body_html = "<br>".join(_math_html(line) for line in (body_text or "").split("\n") if line.strip())
                parts.append(f"<h4>【题目】</h4><p>{body_html}</p>")
                parts.append(_img(shots_dir, ex.shot, f"{ex.id} 题目原图"))
                if ex.answer:
                    parts.append(f'<h4>【参考答案】</h4><p style="font-weight:600;color:#1e3a8a">{_math_html(ex.answer)}</p>')
                steps = [clean_block(s) for s in ex.solution if clean_block(s)]
                if steps:
                    parts.append("<h4>【分步精析】</h4><ol>" + "".join(f"<li>{_math_html(s)}</li>" for s in steps) + "</ol>")
                else:
                    parts.append('<div class="quote">▸ 提示：视频中直接给出答案，讲解时引导学生补充步骤。</div>')
                if ex.notes:
                    parts.append(f'<div class="note">▸ 思路点拨：{_math_html(ex.notes)}</div>')

    if L.common_mistakes:
        parts.append("<h2>四、易错点汇总</h2>" + _bullets([_math_html(x) for x in L.common_mistakes]))

    if L.flow:
        parts.append("<h2>五、课堂流程与时间分配</h2>")
        parts.append(
            '<table class="grid"><tr><th>环节</th><th>时长(分)</th><th>教师活动</th><th>教师话术</th><th>设计意图</th></tr>'
            + "".join(
                f"<tr><td>{_e(s.stage)}</td><td>{s.duration_min:.0f}</td><td>{_e(s.activity)}</td>"
                f"<td>{_e(s.teacher_script)}</td><td>{_e(s.intent)}</td></tr>"
                for s in L.flow
            )
            + "</table>"
        )

    if L.homework:
        parts.append("<h2>六、课后作业与巩固</h2><ol>" + "".join(f"<li>{_math_html(h)}</li>" for h in L.homework) + "</ol>")

    parts.append("<h2>附、教案审核记录</h2>")
    parts.append(
        f'<p class="verdict {r.verdict}">审核结论：{_e(verdict)}　|　得分：{r.score:.0f}　|　回炉轮数：{r.rounds}</p>'
    )
    if r.summary:
        parts.append(f'<div class="note">{_e(r.summary)}</div>')
    return "".join(parts)


def _html_student(L: Lesson, shots_dir: Path) -> str:
    """学生版：讲练结合，精简知识点 + 重点例题演练 + 做题留白 + 附录答案。"""
    m = L.meta
    parts: list[str] = []
    parts.append(f"<h1>{_e(m.title or '教案')}</h1>")
    bits = [b for b in (m.subject, m.grade) if b]
    parts.append(f'<div class="sub">{"　".join(bits)}　·　课堂同步讲义与练习（讲练结合）</div>')

    parts.append("<h2>一、课堂同步精讲与针对演练</h2>")
    for idx, kp in enumerate(L.knowledge, 1):
        parts.append(f'<div style="margin:24px 0 16px;padding:16px;background:#f8fafc;border-radius:10px;border:1px solid #e2e8f0">')
        parts.append(f'<h3 style="margin-top:0;color:#1e40af">第 {idx} 讲：{_math_html(kp.title)}</h3>')
        
        # 知识要点速记
        parts.append('<div style="font-weight:600;font-size:13px;color:#3b82f6;margin-bottom:6px">【知识要点速记】</div>')
        if kp.definition:
            parts.append(f'<p style="margin:4px 0">{_math_html(kp.definition)}</p>')
        if kp.formula_latex:
            pure_f = clean_latex(kp.formula_latex)
            parts.append(f'<div style="margin:8px 0;text-align:center">$${pure_f}$$</div>')
        if kp.key_points:
            parts.append(_bullets([_math_html(x) for x in kp.key_points]))
        elif not kp.definition and not kp.formula_latex and kp.summary:
            parts.append(f'<p>{_math_html(kp.summary)}</p>')
        if kp.board_shot:
            parts.append(_img(shots_dir, kp.board_shot, "板书总结"))

        # 配套针对演练
        if kp.examples:
            parts.append('<div style="font-weight:600;font-size:13.5px;color:#0f766e;margin:18px 0 8px">【即学即练·针对精练】</div>')
            for ex in kp.examples:
                parts.append(f'<h4 style="margin:12px 0 4px">{_e(ex.id)}．{_math_html(ex.title)}</h4>')
                body_text = ex.statement_latex or ex.statement_plain
                body_html = "<br>".join(_math_html(line) for line in (body_text or "").split("\n") if line.strip())
                parts.append(f'<p style="margin:6px 0">{body_html}</p>')
                parts.append(_img(shots_dir, ex.shot, f"{ex.id} 题目原图"))
                # 答题草稿留白区
                parts.append('<div style="border:1.5px dashed #cbd5e1;border-radius:8px;background:#fff;min-height:90px;padding:10px;margin:8px 0 16px;color:#94a3b8;font-size:11.5px">✍ 解题思路与步骤演算区：</div>')
        parts.append('</div>')

    if L.homework:
        parts.append("<h2>二、课后巩固拓展</h2>")
        for hi, h in enumerate(L.homework, 1):
            parts.append(f'<div style="margin:12px 0"><b>习题 {hi}.</b> {_math_html(h)}</div>')
            parts.append('<div style="border:1.5px dashed #cbd5e1;border-radius:8px;background:#fff;min-height:70px;padding:8px;margin:6px 0 12px;color:#94a3b8;font-size:11px">✍ 作答区：</div>')

    parts.append("<h2>三、参考答案与分步解析</h2>")
    parts.append('<details style="background:#f1f5f9;border-radius:8px;padding:12px 16px;margin:12px 0;cursor:pointer"><summary style="font-weight:600;color:#1e40af">▶ 点击展开 / 收起课堂例题参考答案</summary><div style="margin-top:12px">')
    for kp in L.knowledge:
        for ex in kp.examples:
            if ex.answer or ex.solution:
                parts.append(f'<div style="margin:10px 0;padding-bottom:8px;border-bottom:1px solid #e2e8f0"><b>{_e(ex.id)}（{_math_html(ex.title)}）：</b>')
                if ex.answer:
                    parts.append(f'<span style="color:#15803d;font-weight:600"> 答案 【{_math_html(ex.answer)}】</span>')
                steps = [clean_block(s) for s in ex.solution if clean_block(s)]
                if steps:
                    parts.append('<ol style="margin:6px 0;padding-left:20px">' + "".join(f'<li>{_math_html(s)}</li>' for s in steps) + '</ol>')
                parts.append('</div>')
    parts.append('</div></details>')

    return "".join(parts)


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "", text).strip()
    return (cleaned[:60] or "教案")