"""阶段 7：审核。规则体检 + 模型交叉审核 + 自动回炉修订。

两道关：
1. **规则体检**（本地、免费、100% 确定性）：例题缺截图、公式花括号不配对、时间戳越界…
2. **模型交叉审核**：把教案和字幕/题干并排给模型，专查"编造内容"和"公式写错"。

不通过就带着意见回炉重写，最多 MAX_REVIEW_ROUNDS 轮。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from app.config import SETTINGS
from app.llm.client import MiniMaxClient
from app.llm.parallel import run_batches
from app.llm.prompts import (
    REVIEW_SYSTEM,
    REVIEW_USER,
    REVISE_SYSTEM,
    REVISE_USER,
)
from app.models import Cue, Lesson, ReviewIssue, ReviewReport, fmt_ts

Progress = Callable[[float, str], None]


# ------------------------------------------------------------------ 规则体检
_LABEL_RE = re.compile(r"[（(【\[]([^（()【】\[\]]{2,20})[）)】\]]")


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s or "").lower()


def _ungrounded_labels(text: str, transcript_norm: str) -> list[str]:
    """挑出标题里字幕原文从没出现过的括注标签。

    ASR 会把"一个点是一个F2"里的 F2 听成噪声，切分阶段再把它读成"例2"，
    于是标题里就长出视频里根本没出现过的标签。对"一字不差"来说，
    多出来的标签比少一个更致命，所以要当成臆造报出来。
    """
    if not transcript_norm:
        return []
    bad: list[str] = []
    for m in _LABEL_RE.finditer(text or ""):
        inner = _norm(m.group(1))
        if inner and inner not in transcript_norm:
            bad.append(m.group(0))
    return bad


def rule_check(
    lesson: Lesson,
    shots_dir: Path,
    duration: float,
    cues: list[Cue] | None = None,
) -> list[str]:
    """返回问题清单，空列表 = 全部通过。"""
    problems: list[str] = []
    transcript_norm = _norm("".join(c.text for c in cues)) if cues else ""

    if not lesson.objectives:
        problems.append("缺少教学目标")
    if len(lesson.objectives) < 3:
        problems.append(f"教学目标只有 {len(lesson.objectives)} 条，通常需要 3~5 条")
    if not lesson.key_points:
        problems.append("缺少教学重点")
    if not lesson.difficulties:
        problems.append("缺少教学难点")
    if not lesson.knowledge:
        problems.append("没有任何知识点")
    if not lesson.flow:
        problems.append("缺少课堂流程")
    if not lesson.homework:
        problems.append("缺少课后作业")

    for kp in lesson.knowledge:
        if not kp.summary:
            problems.append(f"[{kp.id}] 缺少讲解摘要")
        if not kp.key_points:
            problems.append(f"[{kp.id}] 缺少要点")
        if kp.ts_start < 0 or kp.ts_end > duration + 5:
            problems.append(
                f"[{kp.id}] 时间戳越界：{fmt_ts(kp.ts_start)}–{fmt_ts(kp.ts_end)}，视频长 {fmt_ts(duration)}"
            )
        if kp.ts_end <= kp.ts_start:
            problems.append(f"[{kp.id}] 时间戳倒挂：结束早于开始")
        if kp.formula_latex:
            issue = _latex_issue(kp.formula_latex)
            if issue:
                problems.append(f"[{kp.id}] 核心公式{issue}")
        if kp.board_shot and not (shots_dir / kp.board_shot).exists():
            problems.append(f"[{kp.id}] 板书截图文件不存在：{kp.board_shot}")

        for label in _ungrounded_labels(kp.title, transcript_norm):
            problems.append(
                f"[{kp.id}] 知识点标题里的「{label}」字幕里没有逐字出现，"
                "多半是 ASR 噪声串进来的臆造标签，请删掉或换成字幕里的原话"
            )

        for ex in kp.examples:
            tag = f"[{ex.id}]"
            if not ex.verified:
                problems.append(f"{tag} 例题截图未经多模态确认，题干可能不准")
            if not ex.shot:
                problems.append(f"{tag} 缺少题目截图")
            elif not (shots_dir / ex.shot).exists():
                problems.append(f"{tag} 截图文件不存在：{ex.shot}")
            if not (ex.statement_latex or ex.statement_plain):
                problems.append(f"{tag} 题干为空")
            for label in _ungrounded_labels(ex.title, transcript_norm):
                problems.append(
                    f"{tag} 例题标题里的「{label}」字幕里没有逐字出现，"
                    "多半是 ASR 噪声串进来的臆造标签，请删掉或换成字幕里的原话"
                )
            if not ex.answer:
                # 视频没讲完/老师没报答案时，答案就是不存在。
                # 这种情况下要求模型"补一个答案"＝ 要求它编造，所以只作提示、不算阻塞问题，
                # 否则回炉多少轮都填不满（视频里根本没这个信息）。
                problems.append(
                    f"{tag} 视频里没有听到老师报答案，需要老师自己补（不要让模型编）"
                )
            # 解析不是强制的：网课里老师常常只报答案不讲过程，那份教案就该只有答案，
            # 不能为了凑"解析步骤"而让模型自己编一套出来——那正是幻觉的来源。
            if not ex.solution and ex.notes:
                problems.append(f"{tag} 写了讲解备注却没有解析步骤，请确认老师到底讲没讲")
            issue = _latex_issue(ex.statement_latex)
            if issue:
                problems.append(f"{tag} 题干{issue}")
            for step in ex.solution:
                step_issue = _latex_issue(step)
                if step_issue:
                    problems.append(f"{tag} 解析「{step[:18]}…」{step_issue}")
                    break

    total_min = sum(step.duration_min for step in lesson.flow)
    if lesson.flow and abs(total_min - lesson.meta.duration_min) > max(15.0, lesson.meta.duration_min * 0.5):
        problems.append(
            f"课堂流程各环节时长合计 {total_min:.0f} 分钟，与课时 {lesson.meta.duration_min:.0f} 分钟对不上"
        )

    # 重复例题：这条以前只能靠模型在审核里发现，于是每轮都报、回炉又改不掉。
    # 规则体检能 100% 确定地查出来，别把这种确定性问题交给概率性的模型。
    from app.stages import normalize as _normalize

    problems.extend(_normalize.suspicious_pairs(lesson.knowledge))

    return problems


def _latex_issue(latex: str) -> str:
    """检查一段文本里的 LaTeX 片段有没有明显毛刺。"""
    if not latex:
        return ""
    issues: list[str] = []
    depth = 0
    escaped = False
    for ch in latex:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth < 0:
                return "存在多余的右花括号 }"
    if depth > 0:
        issues.append("花括号未闭合")
    if latex.count("$") % 2 == 1:
        issues.append("$ 定界符不成对")
    begins = latex.count("\\begin{")
    ends = latex.count("\\end{")
    if begins != ends:
        issues.append("\\begin 与 \\end 不配对")
    if "\\(" in latex and "\\)" not in latex:
        issues.append("行内公式 \\( 缺少 \\)")
    return "：".join(issues)


# 命中的就是 _latex_issue 真正产出的字面量。注意 "\\begin" 在普通字符串里
# 只表示**一个**反斜杠；写成 "\\\\begin" 会变成两个，永远匹配不上，
# 公式定界符坏了也只会拿到"补齐这一项"这种驴唇不对马嘴的修改建议。
_BLOCKING_HINTS = (
    "例题截图未经", "题干为空", "缺少答案", "既没有答案",
    "花括号", "$ 定界符", "\\begin", "公式", "截图文件不存在", "缺少题目截图",
    "时间戳越界", "时间戳倒挂", "缺少板书", "字幕里没有逐字出现",
    "疑似同一道题被切成了两道", "题干存在包含关系",
)


def _rule_issue(problem: str) -> ReviewIssue:
    """把规则体检的每一条问题都变成可执行的审核意见（否则回炉会空转）。"""
    severity = "major" if any(h in problem for h in _BLOCKING_HINTS) else "minor"
    target = problem.split("]")[0].strip("[") if "]" in problem else "整体"
    if severity == "major":
        suggestion = "对照字幕原文和例题截图核对后修正，不要凭空补内容"
    else:
        suggestion = "补齐这一项，让教案能直接照着讲"
    return ReviewIssue(severity=severity, target=target, problem=problem, suggestion=suggestion)


def _issue_from(data: Any, default_target: str = "") -> ReviewIssue | None:
    """把模型给的一条意见解析成 ReviewIssue。

    ``kind`` 决定这条算不算编造：模型漏给就按 fact 处理（宁可严）。
    但如果它把 severity 标成 minor 而 kind 标成 supplement，统一按 minor 收，
    免得模型自己标个 blocker 的"补充建议"把整份教案判成 fail。
    """
    if not isinstance(data, dict):
        return None
    kind = str(data.get("kind") or "fact").lower()
    kind = kind if kind in ("fact", "supplement") else "fact"
    sev = str(data.get("severity") or "major").lower()
    if sev not in {"blocker", "major", "minor"}:
        sev = "major"
    if kind == "supplement":
        sev = "minor"
    return ReviewIssue(
        kind=kind,  # type: ignore[arg-type]
        severity=sev,  # type: ignore[arg-type]
        target=str(data.get("target") or default_target),
        problem=str(data.get("problem") or ""),
        suggestion=str(data.get("suggestion") or ""),
    )


# ------------------------------------------------------------------ 模型审核
# 每批给模型审多少个知识点。
# 整份教案 + 全量字幕 + 11 个知识点一次丢进去，review 连撞两次 600s 超时；
# 拆成小批后每次只看自己那几个知识点，推理链短得多。
REVIEW_BATCH = 3


def _lesson_for_review(lesson: Lesson, only_kp_ids: set[str] | None = None) -> str:
    """给审核员看的教案文本。

    ``only_kp_ids`` 非空时只带这几个知识点（顶层字段照带，它们是全局性的）。
    分批评审靠它裁剪——11 个知识点 + 完整字幕一次丢进去，600 秒根本不够用。

    空集合一律当"不筛选"。判断必须是真值而不是 `is not None`：全局核查
    那一遍传的就是 set()，用 `is not None` 会把 knowledge 裁成空数组，
    审核员看到的是一份只剩 flow/homework 的空壳教案，于是把里面
    "引用 kp4 / 例3"判成断裂引用，凭空造出一堆假的事实错误——
    实测直接把一份没问题的教案判成 fail。
    """
    data = lesson.model_dump()
    if only_kp_ids:
        data["knowledge"] = [k for k in data.get("knowledge", []) if k.get("id") in only_kp_ids]
    return json.dumps(data, ensure_ascii=False, indent=2)


def _shots_for_review(lesson: Lesson, shots_dir: Path,
                      only_kp_ids: set[str] | None = None) -> str:
    """列给审核员看的素材清单。

    必须把板书截图也列进去——之前只列例题，审核员看不到就会误判
    "板书截图不存在"（实际文件是有的），白白报一条假问题。
    """
    rows = []
    for kp in lesson.knowledge:
        # 空集合 = 不筛选（全局核查传的就是 set()），和 _lesson_for_review 同一个口径
        if only_kp_ids and kp.id not in only_kp_ids:
            continue
        board = kp.board_shot
        board_ok = (shots_dir / board).exists() if board else False
        rows.append(
            f"- {kp.id}｜{kp.title}｜板书截图={board or '无'}"
            f"（文件{'存在' if board_ok else '未提供'}）"
        )
        for ex in kp.examples:
            exists = (shots_dir / (ex.shot or "")).exists() if ex.shot else False
            rows.append(
                f"- {ex.id}｜{ex.title}｜题目截图={ex.shot or '无'}（文件{'存在' if exists else '缺失'}）"
                f"｜多模态确认={'是' if ex.verified else '否'}｜把握={ex.shot_confidence:.2f}"
            )
    note = "（以上文件是否存在已由本地规则体检核对过，不要凭清单缺失就断定文件没有）"
    return "\n".join(rows) + "\n" + note if rows else "（本课没有知识点）"


def review(
    lesson: Lesson,
    cues: list[Cue],
    shots_dir: Path,
    duration: float,
    progress: Progress | None = None,
) -> ReviewReport:
    client = MiniMaxClient()
    report = ReviewReport()

    problems = rule_check(lesson, shots_dir, duration, cues)
    report.rule_checks = (
        [f"✅ 规则体检全部通过（{len(lesson.knowledge)} 知识点 / {lesson.example_count()} 例题）"]
        if not problems
        else [f"❌ {p}" for p in problems]
    )
    for p in problems:
        report.issues.append(_rule_issue(p))

    if progress:
        progress(0.3, f"规则体检完成，发现 {len(problems)} 个问题；交给模型做事实核查…")

    # 模型审核分批跑：整份教案 + 完整字幕一次丢进去，11 个知识点一起推理，
    # 实测 600 秒扛不住（review 和 refine 都栽在这上面）。
    # 改成每个知识点批次只带自己的内容、字幕切片和截图，
    # 最后再补一次只看顶层字段的全局核查。
    from app.stages.compose import _transcript_slice

    kps = list(lesson.knowledge)
    batches = [kps[i:i + REVIEW_BATCH] for i in range(0, len(kps), REVIEW_BATCH)] or [[]]

    def _ask(ids: set[str] | None, slice_kps: list, label: str, extra_note: str) -> dict | None:
        try:
            data = client.chat_object(
                [{"role": "system", "content": REVIEW_SYSTEM},
                 {"role": "user", "content": REVIEW_USER.format(
                     lesson=_lesson_for_review(lesson, ids),
                     transcript=_transcript_slice(cues, slice_kps) if slice_kps else "",
                     shots=_shots_for_review(lesson, shots_dir, ids),
                 ) + extra_note}],
                temperature=0.1,
                label=label,
            )
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress(0.3, f"⚠ {label} 调用失败：{type(exc).__name__}")
            return None

    failed: list[str] = []
    verdict_rank = {"pass": 0, "revise": 1, "fail": 2}
    worst = "pass"
    scores: list[float] = []
    risks: list[str] = []
    summaries: list[str] = []
    teacher_notes: list[str] = []

    # 各批只审自己的知识点，彼此独立，并发跑。
    # 以前排成一条队，4 批串行要 399~689 秒，一半以上是干等。
    def _ask_batch(group: list, bi: int) -> dict | None:
        no = bi + 1  # run_batches 传的是 0 起下标，日志要给人看的
        ids = {k.id for k in group}
        note = (f"\n\n【重要】这是第 {no}/{len(batches)} 批，只审下面这几个知识点。"
                f"只针对本批内容给结论，不要臆测其它知识点。"
                f"verdict 按本批最严重的问题给。\n")
        try:
            # 每个批次自己建客户端：降级状态是实例级的，共用会互相踩
            data = MiniMaxClient().chat_object(
                [{"role": "system", "content": REVIEW_SYSTEM},
                 {"role": "user", "content": REVIEW_USER.format(
                     lesson=_lesson_for_review(lesson, ids),
                     transcript=_transcript_slice(cues, group) if group else "",
                     shots=_shots_for_review(lesson, shots_dir, ids),
                 ) + note}],
                temperature=0.1,
                label=f"review-{no}/{len(batches)}",
            )
            return data if isinstance(data, dict) else None
        except Exception as exc:  # noqa: BLE001
            if progress:
                progress(0.3, f"⚠ 第 {bi} 批审核调用失败：{type(exc).__name__}")
            return None

    def _batch_progress(done: int, total: int) -> None:
        if progress:
            progress(0.3 + 0.5 * done / total,
                     f"模型审核 {done}/{total} 批完成"
                     + (f"（并发 {SETTINGS.llm_parallel} 路）" if total > 1 else ""))

    results = run_batches(
        batches, _ask_batch, label="review",
        on_progress=_batch_progress, max_workers=SETTINGS.llm_parallel,
    )

    for bi, data in enumerate(results, 1):
        if data is None:
            failed.append(f"第 {bi} 批")
            continue
        v = str(data.get("verdict") or "revise").lower()
        v = v if v in verdict_rank else "revise"
        if verdict_rank[v] > verdict_rank[worst]:
            worst = v
        try:
            scores.append(float(data.get("score") or 0))
        except (TypeError, ValueError):
            pass
        for key in ("hallucination_risk", "summary", "teacher_notes"):
            val = str(data.get(key) or "").strip()
            if not val:
                continue
            if key == "hallucination_risk":
                risks.append(val)
            elif key == "summary":
                summaries.append(val)
            else:
                teacher_notes.append(val)
        for item in data.get("issues") or []:
            issue = _issue_from(item)
            if issue is not None:
                report.issues.append(issue)

    if progress:
        progress(0.85, f"模型审核 {len(batches)} 批完成，{len(report.issues)} 条意见；再核一遍整体一致性…")
    overall = _ask(
        set(), [],
        "review-global",
        "\n\n【重要】这一遍**只看顶层字段**（标题 / 教学目标 / 重点 / 难点 / 作业 / 课堂流程）"
        "与各知识点标题之间的**一致性**，不要重复审知识点正文和例题。\n",
    )

    if failed:
        # 有批次没跑成：如实说清楚，别假装审过了。
        report.model_review_ok = not failed
        report.verdict = "pass" if not problems else "revise"
        report.hallucination_risk = "未评估"
        report.score = 0.0
        report.summary = (
            f"模型交叉审核未能全部完成（失败：{'、'.join(failed)}），"
            f"仅完成本地规则体检，结果："
            f"{'全部通过' if not problems else f'发现 {len(problems)} 个问题'}。"
            "编造风险与公式正确性尚未经完整核查，建议对照原视频复核。"
        )
        if progress:
            progress(1.0, "⚠ 模型审核未完成，已按规则体检结果给出结论")
        return report

    if overall is not None:
        v = str(overall.get("verdict") or "").lower()
        v = v if v in verdict_rank else worst
        if verdict_rank[v] > verdict_rank[worst]:
            worst = v
        try:
            scores.append(float(overall.get("score") or 0))
        except (TypeError, ValueError):
            pass
        for key, bucket in (("hallucination_risk", risks), ("summary", summaries),
                            ("teacher_notes", teacher_notes)):
            val = str(overall.get(key) or "").strip()
            if val:
                bucket.append(val)
        for item in overall.get("issues") or []:
            issue = _issue_from(item, default_target="整体")
            if issue is not None:
                report.issues.append(issue)

    report.model_review_ok = True
    report.verdict = worst  # type: ignore[assignment]
    report.score = round(sum(scores) / len(scores), 1) if scores else 0.0
    # 风险取最严重的那个，别用"低"掩盖某一批的高风险
    order = {"low": 0, "medium": 1, "high": 2, "未评估": 3}
    report.hallucination_risk = max(risks, key=lambda r: order.get(r, 1)) if risks else "unknown"
    report.summary = " ".join(summaries)
    report.teacher_notes = "\n".join(teacher_notes)

    if problems and report.verdict == "pass":
        report.verdict = "revise"

    # 安全网：一条事实错误都没有就别判 revise/fail。
    # 模型有时还是会把口诀、作业题当成缺陷报上来（judgement 飘），
    # 那类意见不该让整份教案永远过不了关——补充建议单独列出来给老师看。
    fact_issues = [i for i in report.issues if i.is_fact]
    if not fact_issues and not problems:
        report.verdict = "pass"

    supp = len(report.issues) - len(fact_issues)
    report.stats_note = (
        f"事实错误 {len(fact_issues)} 条，教学补充 {supp} 条"
        + ("（教学补充不算缺陷，供老师取舍）" if supp else "")
    )
    if progress:
        progress(1.0, f"审核完成：{report.verdict}"
                        f"（事实错误 {len(fact_issues)} 条 / 教学补充 {supp} 条）")
    return report


# ------------------------------------------------------------------ 回炉修订
# 每批给模型多少条意见。一批太多会让一次调用要吐的东西太大而撞 600s 超时。
REFINE_BATCH = 8


def _issue_kp_ids(issues: list[ReviewIssue], lesson: Lesson) -> set[str] | None:
    """从一批意见里定位它真正涉及的知识点，回炉时只把这些发给模型。

    以前每一批都把整份教案（11 个知识点 + 12 道例题）重发一遍——拆成 4 批
    只是把同一份大报文发了 4 遍，MiniMax-M3 上 600s 连撞三次。
    裁剪后单批报文能缩掉一大半。

    定位不到就返回 None（发全量）：宁可多发也不能少发，
    漏发的知识点模型就改不到，那条意见等于白提。
    """
    kp_ids = {kp.id for kp in lesson.knowledge}
    ex_owner: dict[str, str] = {
        ex.id: kp.id for kp in lesson.knowledge for ex in kp.examples
    }
    known = kp_ids | set(ex_owner)
    if not known:
        return None
    hit: set[str] = set()
    for issue in issues:
        blob = f"{issue.target}\n{issue.problem}\n{issue.suggestion}"
        for tok in known:
            # 加词边界，否则 "ex1" 会被 "ex10" 误命中
            if re.search(rf"(?<![0-9A-Za-z_]){re.escape(tok)}(?![0-9A-Za-z_])", blob):
                hit.add(ex_owner.get(tok, tok))
    return hit or None


def _content_score(lesson: Lesson) -> int:
    """粗略衡量"这份教案有多少实际内容"，用来判断修订是不是把东西洗掉了。"""
    score = (len(lesson.objectives) + len(lesson.key_points) + len(lesson.difficulties)
             + len(lesson.prerequisites) + len(lesson.materials)
             + len(lesson.common_mistakes) + len(lesson.homework)
             + len(lesson.flow))
    for kp in lesson.knowledge:
        score += 3 if kp.summary else 0
        score += 2 if kp.definition else 0
        score += 2 if kp.formula_latex else 0
        score += len(kp.key_points) + len(kp.pitfalls)
        score += 3 if kp.teaching_notes else 0
        for ex in kp.examples:
            score += 2 if ex.answer else 0
            score += len(ex.solution)
            score += 2 if (ex.statement_latex or ex.statement_plain) else 0
    return score


def refine(
    lesson: Lesson,
    report: ReviewReport,
    cues: list[Cue],
    shots_dir: Path,
    duration: float,
    round_no: int,
    progress: Callable[[float, str], None] | None = None,
) -> tuple[Lesson, ReviewReport]:
    """按审核意见重写教案。返回 (新教案, 新审核)。"""
    from app.stages import compose as compose_stage

    client = MiniMaxClient()
    # 严重级别只是排序权重，不是筛选条件。
    # 以前只把 blocker/major 喂给模型，导致"缺少课后作业"这类 minor 意见
    # 永远传不回炉里，成品里就一直留着同样的洞。
    order = {"blocker": 0, "major": 1, "minor": 2}
    # 只修事实错误。教学补充（口诀、作业、一般公式）是老师自己会加的东西，
    # 本来就不该有"错"——把它们喂回炉，模型会越改越离谱。
    facts = [i for i in report.issues if i.is_fact]
    todo = sorted(facts, key=lambda i: order.get(i.severity, 1))[:25]

    # 一批一批改。
    # 原来把 20 多条意见 + 整份教案丢给一次调用、要求重写全部，实测 601s 被硬切断，
    # 重试 5 次全灭。现在每批只让模型输出补丁（改动的那几处），漏掉的字段
    # 由 _to_lesson 的兜底链（新值 → 上一版 → 原始切分）自动沿用。
    batches = [todo[i:i + REFINE_BATCH] for i in range(0, len(todo), REFINE_BATCH)] or [[]]

    current = lesson
    for bi, group in enumerate(batches, 1):
        issues_text = "\n".join(
            f"[{i.severity}] {i.target or '整体'}：{i.problem}"
            + (f"\n    建议：{i.suggestion}" if i.suggestion else "")
            for i in group
        )
        if progress:
            progress(0.05 + 0.35 * (bi - 1) / len(batches),
                     f"第 {round_no} 轮回炉修订 {bi}/{len(batches)}"
                     f"（本批 {len(group)} 条 / 共 {len(todo)} 条）…")

        # 只把这批意见真正涉及的知识点发给模型，其余靠 _to_lesson 的兜底链原样保留。
        scope = _issue_kp_ids(group, current)
        data = client.chat_object(
            [
                {"role": "system", "content": REVISE_SYSTEM},
                {"role": "user", "content": REVISE_USER.format(
                    lesson=_lesson_for_review(current, scope),
                    round=round_no,
                    total=len(todo),
                    part=f"{sum(len(g) for g in batches[:bi - 1]) + 1}~"
                         f"{sum(len(g) for g in batches[:bi])}",
                    issues=issues_text,
                )},
            ],
            temperature=0.25,
            label=f"revise-{bi}/{len(batches)}",
        )

        candidate = compose_stage._to_lesson(
            data, current.meta, current.knowledge, current.meta.duration_min, fallback=current,
        )
        candidate.touch()

        # 保险丝：这一批改完"信息量"要是明显不如改之前，说明把内容洗掉了，
        # 那就丢掉这批修订、保留改之前的版本。宁可这批没改，也不交一份被削空的教案。
        before, after = _content_score(current), _content_score(candidate)
        if after < before * 0.9:
            if progress:
                progress(0.4, f"⚠ 第 {bi} 批修订让内容变少了（{before} → {after}），这批不采纳")
            continue
        current = candidate

    new_lesson = current

    if progress:
        progress(0.5, "修订完成，重新审核…")
    new_report = review(new_lesson, cues, shots_dir, duration, progress)
    return new_lesson, new_report


def run_review_loop(
    lesson: Lesson,
    cues: list[Cue],
    shots_dir: Path,
    duration: float,
    max_rounds: int | None = None,
    progress: Progress | None = None,
    on_lesson: Callable[[Lesson], None] | None = None,
) -> Lesson:
    """审核 → 不通过就回炉 → 再审核，直到通过或用完轮数。

    ``on_lesson`` 每轮回炉完调用一次，让调用方把中间结果落盘。
    一整轮审核十几分钟，机器休眠或进程被杀之后重跑不至于全丢——
    这正是之前连续两次白跑一小时的原因。
    """
    max_rounds = SETTINGS.max_review_rounds if max_rounds is None else max_rounds
    report = review(lesson, cues, shots_dir, duration, progress)
    report.rounds = 0
    lesson.review = report
    if on_lesson:
        on_lesson(lesson)

    round_no = 0
    stalled = False
    while report.verdict != "pass" and round_no < max_rounds:
        fact_issues = [i for i in report.issues if i.is_fact]
        if not fact_issues:
            # 没有事实错误就不用改。剩下的只是教学补充，老师自己取舍。
            report.summary = (
                (report.summary + " ")
                + f"📝 共 {len(report.issues) - len(fact_issues)} 条教学补充建议"
                "（口诀、作业、一般公式等），不属于错误，供老师自行取舍。"
            ).strip()
            lesson.review = report
            break
        round_no += 1
        before_issues = len([i for i in report.issues if i.is_fact])
        lesson, report = refine(lesson, report, cues, shots_dir, duration, round_no, progress)
        report.rounds = round_no
        lesson.review = report
        if on_lesson:
            on_lesson(lesson)
        if progress:
            progress(1.0, f"第 {round_no} 轮审核：{report.verdict}")

        # 回炉没让问题明显变少，就别再烧一轮了。
        # 实测过一轮 35 条、改完还是 35 条的情况——再来一轮同样是 600 秒起步的
        # 审核 + 4 批修订，十几分钟换一份一模一样的结论，纯浪费。
        # 停下来之前如实告诉老师"自动修订没解决问题"，而不是默默跑完再交。
        after_issues = len([i for i in report.issues if i.is_fact])
        if round_no < max_rounds and after_issues >= before_issues * 0.9:
            report.summary = (
                (report.summary + " ")
                + f"⚠ 回炉 {round_no} 轮后事实错误没有减少（{before_issues} → {after_issues}），"
                "已停止自动修订，避免再花十几分钟得到同样的结论。"
                "上面这些 ⚠ 需要老师人工过一遍。"
            ).strip()
            lesson.review = report
            stalled = True
            break

    if report.verdict != "pass" and not stalled:
        report.summary = (
            (report.summary + " ")
            + f"⚠ 已回炉 {round_no} 轮仍未完全通过，文中标注 ⚠ 的地方请老师人工复核。"
        ).strip()
        lesson.review = report

    lesson.stats.update({
        "review_rounds": round_no,
        "review_stalled": bool(stalled),
        "review_verdict": report.verdict,
        "review_score": report.score,
    })
    return lesson