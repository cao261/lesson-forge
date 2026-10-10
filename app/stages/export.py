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
    body = _html_body(lesson, audience=audience)
    label = "配套讲义" if audience == "student" else "教师备课版"
    page = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(lesson.meta.title or '教案')}</title>
<style>{CSS}</style>
<script>
window.MathJax = {{ tex: {{ inlineMath: [['$','$']], displayMath: [['$$','$$']] }}, chtml: {{ scale: 1.0 }} }};
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


def _e(text: str) -> str:
    """转义前先清洗，保证预览里也不会冒出 think/围栏/JSON 残留。"""
    return html.escape(clean_block(text or ""))


def _html_body(L: Lesson, audience: str = "teacher") -> str:
    if audience == "student":
        return _html_student(L)
    m = L.meta
    r = L.review
    verdict = {"pass": "✅ 通过", "revise": "⚠ 已修订，建议人工复核", "fail": "❌ 未通过"}.get(r.verdict, r.verdict)
    parts: list[str] = []

    parts.append(f"<h1>{_e(m.title or '教案')}</h1>")
    parts.append('<div class="sub">教师备课版 · 知识点 / 例题 / 讲法提示</div>')

    meta_rows = [
        ("学科", m.subject), ("年级", m.grade), ("建议课时", f"{m.duration_min:.0f} 分钟"),
        ("授课教师", m.teacher or "＿＿＿＿"), ("学生", m.student or "＿＿＿＿"),
        ("视频来源", m.source_title), ("UP 主", m.uploader),
        ("视频时长", fmt_ts(m.video_duration)), ("原视频链接", m.source_url),
        ("审核结论", f"{verdict}（{r.rounds} 轮回炉）"),
    ]
    parts.append(
        '<table class="meta">'
        + "".join(f'<tr><td class="k">{_e(k)}</td><td>{_e(v)}</td></tr>' for k, v in meta_rows)
        + "</table>"
    )

    parts.append("<h2>一、本课知识点</h2>")
    for idx, kp in enumerate(L.knowledge, 1):
        parts.append(
            f'<h3>{idx}. {_e(kp.title)} '
            f'<span class="ts">[{fmt_ts(kp.ts_start)} – {fmt_ts(kp.ts_end)}]</span></h3>'
        )
        if kp.summary:
            parts.append(f'<div class="note"><b>讲了什么：</b>{_e(kp.summary)}</div>')
        if kp.definition:
            parts.append(f'<div class="note"><b>定义／结论：</b>{_e(kp.definition)}</div>')
        if kp.formula_latex:
            parts.append(f'<p><b>核心公式：</b></p><div>$${kp.formula_latex}$$</div>')
        if kp.key_points:
            parts.append("<h4>要点</h4>" + _bullets([_e(x) for x in kp.key_points]))
        if kp.pitfalls:
            parts.append("<h4>注意／易错</h4>" + _bullets([_e(x) for x in kp.pitfalls]))
        if kp.teaching_notes:
            parts.append(f'<div class="note"><b>▸ 讲法提示：</b>{_e(kp.teaching_notes)}</div>')
        if kp.source_quote:
            parts.append(f'<div class="quote">字幕依据：「{_e(kp.source_quote)}」</div>')
        if kp.board_shot:
            parts.append(_img(_IMG_DIR, kp.board_shot, "板书要点（本知识点总结画面）"))
        if kp.examples:
            parts.append("<h4>本节例题</h4>")
            for ex in kp.examples:
                bits = " · ".join(filter(None, [
                    f"[{fmt_ts(ex.ts_start)}–{fmt_ts(ex.ts_end)}]",
                    "、".join(ex.tags) if ex.tags else "",
                    ex.difficulty if ex.difficulty else "",
                ]))
                parts.append(f'<p><b>{_e(ex.id)}．{_e(ex.title)}</b> <span class="ts">{_e(bits)}</span></p>')
                if not ex.verified:
                    parts.append('<div class="warn">⚠ 题干未经画面确认，请对照视频复核。</div>')
                body = ex.statement_latex or ex.statement_plain
                parts.append("<p>" + "<br>".join(_e(l) for l in (body or "").split("\n") if l.strip()) + "</p>")
                parts.append(_img(_IMG_DIR, ex.shot, f"{ex.id} 题目原图"))
                if ex.answer:
                    parts.append(f"<p><b>答案：</b>{_e(ex.answer)}</p>")

    if L.example_count():
        parts.append("<h2>二、例题精讲</h2>")
        for kp in L.knowledge:
            for ex in kp.examples:
                bits = "　".join(filter(None, [
                    f"[{fmt_ts(ex.ts_start)}–{fmt_ts(ex.ts_end)}]",
                    f"难度：{ex.difficulty}" if ex.difficulty else "",
                    f"考点：{'、'.join(ex.tags)}" if ex.tags else "",
                ]))
                parts.append(f'<h3>{_e(ex.id)}．{_e(ex.title)} <span class="ts">{_e(bits)}</span></h3>')
                if not ex.verified:
                    parts.append('<div class="warn">⚠ 该题截图未通过画面确认，题干可能不完整，请对照视频复核。</div>')
                body_text = ex.statement_latex or ex.statement_plain
                body_html = "<br>".join(_e(line) for line in (body_text or "").split("\n") if line.strip())
                parts.append(f"<h4>【题目】</h4><p>{body_html}</p>")
                parts.append(_img(_IMG_DIR, ex.shot, "原视频画面"))
                if ex.answer:
                    parts.append(f"<h4>【答案】</h4><p>{_e(ex.answer)}</p>")
                steps = [clean_block(s) for s in ex.solution if clean_block(s)]
                if steps:
                    parts.append("<h4>【解析】</h4><ol>" + "".join(f"<li>{s}</li>" for s in steps) + "</ol>")
                else:
                    parts.append('<div class="quote">▸ 视频里老师只报了答案，没有展开讲解。</div>')
                if ex.notes:
                    parts.append(f'<div class="note">▸ 提示：{_e(ex.notes)}</div>')

    if L.objectives:
        parts.append("<h2>三、教学目标</h2>" + _bullets([_e(x) for x in L.objectives]))

    parts.append("<h2>四、教学重点与难点</h2>")
    if L.key_points:
        parts.append("<h4>教学重点</h4>" + _bullets([_e(x) for x in L.key_points]))
    if L.difficulties:
        parts.append("<h4>教学难点</h4>" + _bullets([_e(x) for x in L.difficulties]))
    if L.prerequisites:
        parts.append("<h4>前置知识</h4>" + _bullets([_e(x) for x in L.prerequisites]))

    if L.common_mistakes:
        parts.append("<h2>五、易错点汇总</h2>" + _bullets([_e(x) for x in L.common_mistakes]))

    if L.materials:
        parts.append("<h2>六、教学准备</h2>" + _bullets([_e(x) for x in L.materials]))

    if L.flow:
        parts.append("<h2>七、课堂流程与时间分配</h2>")
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
        parts.append("<h2>八、课后作业</h2><ol>" + "".join(f"<li>{_e(h)}</li>" for h in L.homework) + "</ol>")

    parts.append("<h2>附、审核记录</h2>")
    parts.append(
        f'<p class="verdict {r.verdict}">审核结论：{_e(verdict)}　|　得分：{r.score:.0f}　|　回炉轮数：{r.rounds}</p>'
    )
    if not r.model_review_ok:
        parts.append('<div class="warn">⚠ 模型交叉审核未完成，本节结论仅来自本地规则体检。</div>')
    if r.hallucination_risk:
        parts.append(f"<p>编造风险评估：{_e(r.hallucination_risk)}</p>")
    if r.summary:
        parts.append(f'<div class="note">{_e(r.summary)}</div>')
    if r.rule_checks:
        parts.append("<h4>规则体检</h4>" + _bullets([_e(x) for x in r.rule_checks]))
    if r.issues:
        rows = "".join(
            f'<tr class="marker"><td>[{_e(i.target or "整体")}]</td><td>{_e(i.problem)}</td><td>{_e(i.suggestion)}</td></tr>'
            for i in r.issues
        )
        parts.append(f'<h4>问题清单</h4><table class="grid"><tr><th>位置</th><th>问题</th><th>建议</th></tr>{rows}</table>')
    if r.teacher_notes:
        parts.append(f'<div class="note">▸ 审核老师提醒：{_e(r.teacher_notes)}</div>')
    return "".join(parts)


def _html_student(L: Lesson) -> str:
    """学生版：只有粗略知识点和例题（题干原图 + 答案）。

    刻意不出现讲法提示、易错提醒、审核记录——那些是老师盯着补充的部分，
    摆在学生面前只会让人分心。
    """
    m = L.meta
    parts: list[str] = []
    parts.append(f"<h1>{_e(m.title or '教案')}</h1>")
    bits = [b for b in (m.subject, m.grade) if b]
    parts.append(f'<div class="sub">{"　".join(bits)}　·　配套讲义</div>')

    parts.append("<h2>一、本课知识点</h2>")
    for idx, kp in enumerate(L.knowledge, 1):
        parts.append(f"<h3>{idx}. {_e(kp.title)}</h3>")
        if kp.definition:
            parts.append(f'<div class="note">{_e(kp.definition)}</div>')
        if kp.formula_latex:
            parts.append(f"<div>$${kp.formula_latex}$$</div>")
        if kp.key_points:
            parts.append(_bullets([_e(x) for x in kp.key_points]))
        elif not kp.definition and not kp.formula_latex:
            parts.append(f"<p>{_e(kp.summary)}</p>")
        if kp.board_shot:
            parts.append(_img(_IMG_DIR, kp.board_shot, "板书要点"))
        if kp.examples:
            parts.append(f'<p class="ts">本节例题：{_e("、".join(e.title for e in kp.examples))}</p>')

    if L.example_count():
        parts.append("<h2>二、例题</h2>")
        for kp in L.knowledge:
            for ex in kp.examples:
                parts.append(f"<h3>{_e(ex.id)}．{_e(ex.title)}</h3>")
                body_text = ex.statement_latex or ex.statement_plain
                body_html = "<br>".join(_e(line) for line in (body_text or "").split("\n") if line.strip())
                parts.append(f"<p>{body_html}</p>")
                parts.append(_img(_IMG_DIR, ex.shot, "题目原图"))
                if ex.answer:
                    parts.append(f'<p style="font-size:15px"><b>答案：</b>{_e(ex.answer)}</p>')

    if L.homework:
        parts.append("<h2>三、课后作业</h2><ol>" + "".join(f"<li>{_e(h)}</li>" for h in L.homework) + "</ol>")
    return "".join(parts)


_IMG_DIR = Path(".")


def set_image_dir(path: Path) -> None:
    global _IMG_DIR
    _IMG_DIR = path


def _safe_name(text: str) -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", "", text).strip()
    return (cleaned[:60] or "教案")