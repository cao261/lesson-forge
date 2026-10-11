"""阶段 6：让模型以"教案编写助教"的身份，基于字幕 + 题干生成完整教案。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from app import textnorm
from app.config import SETTINGS
from app.llm.client import LLMError, MiniMaxClient
from app.llm.parallel import run_batches
from app.llm.prompts import (
    COMPOSE_EXAMPLES_SYSTEM,
    COMPOSE_EXAMPLES_USER,
    COMPOSE_OUTLINE_USER,
    COMPOSE_SYSTEM,
    knowledge_for_prompt,
)
from app.models import Cue, FlowStep, KnowledgePoint, Lesson, LessonMeta, fmt_ts

Progress = Callable[[float, str], None]

DEFAULT_DURATION_MIN = 90.0

# 一发里最多塞几道例题。实测 9 知识点 + 11 例题一次生成会被 600s 超时硬切断，
# 拆成每发几道之后每发都能在几分钟内跑完。
EXAMPLES_PER_CALL = 3
KPS_PER_CALL = 3


def _chunk_kps(kps: list[KnowledgePoint]) -> list[list[KnowledgePoint]]:
    """按例题数量和知识点数量双限制分批，两边都不能超。"""
    chunks: list[list[KnowledgePoint]] = []
    cur: list[KnowledgePoint] = []
    cur_ex = 0
    for kp in kps:
        n = len(kp.examples)
        # 单个知识点例题就超限的话，单独给它一批，宁可一批大也不能拆散一道题
        too_big = cur and (cur_ex + n > EXAMPLES_PER_CALL or len(cur) >= KPS_PER_CALL)
        if too_big:
            chunks.append(cur)
            cur, cur_ex = [], 0
        cur.append(kp)
        cur_ex += n
    if cur:
        chunks.append(cur)
    return chunks


def _transcript_slice(cues: list[Cue], kps: list[KnowledgePoint]) -> str:
    """只给这几个知识点时间范围内的字幕，省 token 也更聚焦。"""
    if not kps:
        return ""
    lo = min(k.ts_start for k in kps)
    hi = max(k.ts_end for k in kps)
    picked = [c for c in cues if c.start <= hi and c.end >= lo]
    if len(picked) < 40:  # 范围太窄反而漏内容，就给全文
        picked = cues
    return "\n".join(f"[{fmt_ts(c.start)}] {c.text}" for c in picked)


def _merge_examples(data: object, ex_map: dict[str, list[dict]]) -> dict:
    """把分批拿到的例题按 kp id 挂回骨架（骨架负责知识点本身）。"""
    if not ex_map or not isinstance(data, dict):
        return data if isinstance(data, dict) else {}
    rows = []
    for row in data.get("knowledge") or []:
        if isinstance(row, dict):
            got = ex_map.get(str(row.get("id") or ""))
            if got:
                row = {**row, "examples": got}
            rows.append(row)
    return {**data, "knowledge": rows}


def _degenerate(lesson: Lesson) -> str:
    """判断这份教案是不是"空壳"。返回空串 = 正常，否则说明哪里塌了。

    必须有这道闸：模型偶尔会"成功"返回一个字段齐全但全是空串的 JSON
    （实测 compose-outline 166 秒返回、零报错，结果教学目标/流程/所有知识点
    摘要/所有例题答案全是空的）。这种东西一路传下去，
    审核最后报 60 个问题、成品什么也教不了，而根因在几十分钟前的 compose。

    只拦"什么都没有"这种致命情况。少写一条教学目标、少几个要点不算致命——
    那些规则体检本来就会报，也能靠回炉补；这里一票否决只会误伤。
    """
    reasons: list[str] = []
    filled_kp = sum(1 for kp in lesson.knowledge if kp.summary or kp.definition)
    if lesson.knowledge and filled_kp == 0:
        reasons.append(f"{len(lesson.knowledge)} 个知识点全都没有摘要/定义")
    total_ex = lesson.example_count()
    answered = sum(
        1 for kp in lesson.knowledge for e in kp.examples if e.answer or e.solution
    )
    if total_ex and answered == 0:
        reasons.append(f"{total_ex} 道例题全都没有答案或解析")
    if not reasons:
        return ""
    # 把"教学目标/流程也空"作为佐证附在后面，帮人看懂这次是彻底塌了
    extra = [n for n, c in (("教学目标为空", not lesson.objectives),
                            ("课堂流程为空", not lesson.flow)) if c]
    return "；".join(reasons + extra)


def compose(
    meta: LessonMeta,
    kps: list[KnowledgePoint],
    cues: list[Cue],
    duration_min: float = DEFAULT_DURATION_MIN,
    progress: Progress | None = None,
    on_partial: Callable[[Lesson], None] | None = None,
) -> Lesson:
    client = MiniMaxClient()
    transcript = "\n".join(f"[{fmt_ts(c.start)}] {c.text}" for c in cues)
    n_ex = sum(len(k.examples) for k in kps)

    def say(frac: float, msg: str) -> None:
        if progress:
            progress(frac, msg)

    # ---- 第一发：教案骨架（目标/流程/知识点讲解），不含例题 ----
    say(0.2, f"生成教案骨架（知识点 {len(kps)} 个）…")
    outline_msg = COMPOSE_OUTLINE_USER.format(
        meta=meta,
        video_duration=fmt_ts(meta.video_duration),
        duration_min=duration_min,
        knowledge=knowledge_for_prompt(kps),
        transcript=transcript,
    )
    data = client.chat_object(
        [{"role": "system", "content": COMPOSE_SYSTEM},
         {"role": "user", "content": outline_msg}],
        temperature=0.3,
        label="compose-outline",
    )

    # ---- 后续每发：按知识点分批补例题 ----
    chunks = _chunk_kps(kps)
    ex_map: dict[str, list[dict]] = {}
    if n_ex and chunks:
        # 各批之间完全独立（各写各的知识点），并发跑。
        # 以前排成一条队，5 批串行要 450 秒以上，全是干等。
        def _one_ex_batch(group: list[KnowledgePoint], i: int) -> dict | None:
            # 每个批次自己建客户端：降级状态是实例级的，共用会互相踩
            sub = MiniMaxClient()
            ex_msg = COMPOSE_EXAMPLES_USER.format(
                meta=meta,
                knowledge=knowledge_for_prompt(group),
                transcript=_transcript_slice(cues, group),
            )
            try:
                part = sub.chat_object(
                    [{"role": "system", "content": COMPOSE_EXAMPLES_SYSTEM},
                     {"role": "user", "content": ex_msg}],
                    temperature=0.3,
                    label=f"compose-ex-{i + 1}/{len(chunks)}",
                )
            except Exception:  # noqa: BLE001
                return None
            # 拿到结果就立刻并进 ex_map。必须在这里做，不能等 run_batches
            # 返回后再统一 harvest——中途存档是批次**运行期间**触发的，
            # 那时 ex_map 还空着，落盘的就是一份没有例题的骨架；下次重跑
            # _load_draft 的两道闸都拦不住它（骨架不算 degenerate、
            # 学科年级时长又都一致），会被当成合法草稿直接复用。
            _harvest(part)
            return part

        ex_lock = threading.Lock()

        def _harvest(part: object) -> None:
            """把一批返回的知识点例题并进 ex_map（线程安全）。"""
            if not isinstance(part, dict):
                return
            try:
                rows = part.get("knowledge") or []
            except BaseException:  # noqa: BLE001
                return
            with ex_lock:
                for row in rows:
                    if isinstance(row, dict) and row.get("examples"):
                        ex_map[str(row.get("id") or "")] = [
                            e for e in row["examples"] if isinstance(e, dict)
                        ]

        def _snapshot() -> None:
            """每批完成就落一份部分草稿。

            为什么必须中途存：例题批次要跑好几分钟，中途任何一批全灭
            都会让整个 compose 抛异常，此前骨架那 147 秒的算力就白费了。
            中途存档后重跑能接着用，不用从头再来。

            这里必须吞掉**所有**异常（含 LLMError）。存档是锦上添花，
            绝不能因为它把整条任务带崩——踩过一次：存档里构造 Lesson 失败，
            异常一路冒到 run_batches 的 worker，整轮 compose 白跑。
            """
            if not on_partial:
                return
            try:
                with ex_lock:
                    merged = _merge_examples(data, dict(ex_map))
                on_partial(_to_lesson(merged, meta, kps, duration_min))
            except BaseException:  # noqa: BLE001
                return

        def _ex_progress(done: int, total: int) -> None:
            say(0.35 + 0.4 * done / total,
                f"生成例题 {done}/{total} 批完成"
                + (f"（共 {n_ex} 道，并发 {SETTINGS.llm_parallel} 路）" if total > 1 else ""))
            _snapshot()

        parts = run_batches(
            chunks, _one_ex_batch, label="compose-ex",
            on_progress=_ex_progress, max_workers=SETTINGS.llm_parallel,
        )
        for part in parts:
            _harvest(part)
        missing_kps = [kp.id for kp in kps if not ex_map.get(kp.id)]
        if missing_kps:
            say(0.76, f"⚠ {len(missing_kps)} 个知识点的例题这轮没拿到"
                      f"（{'、'.join(missing_kps[:5])}…），会留给规则体检报错")

    # 合并：骨架负责知识点，例题分批结果按 kp id 挂回去
    data = _merge_examples(data, ex_map)

    say(0.85, "校对例题与截图的对应关系…")
    lesson = _to_lesson(data, meta, kps, duration_min)
    lesson.touch()

    bad = _degenerate(lesson)
    if bad:
        # 宁可多花两分钟重做，也不能把一份空壳教案交给审核——那会让审核报出
        # 几十条"内容缺失"，而根因在这里，再怎么回炉也补不回来。
        raise LLMError(
            f"生成的教案初稿是空壳（{bad}），已中止。"
            "通常是模型这一次返回异常，重跑本任务即可；"
            "如果反复出现，换一个模型档位试试。"
        )

    say(1.0, f"教案初稿完成：{lesson.example_count()} 道例题")
    return lesson


def _to_lesson(
    data: dict,
    meta: LessonMeta,
    kps: list[KnowledgePoint],
    duration_min: float,
    fallback: Lesson | None = None,
) -> Lesson:
    """把模型的 JSON 变成 Lesson。

    ``fallback`` 是修订前的上一版。**每个字段都必须能回退到它**——
    模型修订时经常只重写一部分字段（甚至因为截断漏掉一大半），
    实测出现过第二轮修订把教学目标、课堂流程、课后作业、例题答案全洗成空的情况，
    比不修订还糟糕。缺什么就沿用上一版，宁可旧一点也不能丢。
    """
    # 兜底：模型偶尔把教案包成数组返回。解包失败也别让整条流水线挂掉，
    # 退回空教案，让审核环节如实报"内容缺失"。
    if isinstance(data, list):
        dicts = [x for x in data if isinstance(x, dict)]
        data = max(dicts, key=len) if dicts else {}
    if not isinstance(data, dict):
        data = {}

    prev_kp = {k.id: k for k in (fallback.knowledge if fallback else [])}
    prev_ex = {e.id: e for k in (fallback.knowledge if fallback else []) for e in k.examples}

    def keep(new, old, default=None):
        """新值有就用新的，空的就沿用旧的，再空才用默认。"""
        if new:
            return new
        if old:
            return old
        return default if default is not None else old

    lesson = Lesson()
    lesson.meta = meta.model_copy()
    raw_meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    if raw_meta.get("title"):
        lesson.meta.title = str(raw_meta["title"])
    lesson.meta.duration_min = _num(raw_meta.get("duration_min"), duration_min) or duration_min

    f = fallback or Lesson()
    lesson.objectives = _str_list(data.get("objectives")) or f.objectives
    lesson.key_points = _str_list(data.get("key_points")) or f.key_points
    lesson.difficulties = _str_list(data.get("difficulties")) or f.difficulties
    lesson.prerequisites = _str_list(data.get("prerequisites")) or f.prerequisites
    lesson.materials = _str_list(data.get("materials")) or f.materials
    lesson.common_mistakes = _str_list(data.get("common_mistakes")) or f.common_mistakes
    lesson.homework = _str_list(data.get("homework")) or f.homework

    lesson.flow = [
        FlowStep(
            stage=str(item.get("stage") or ""),
            duration_min=_num(item.get("duration_min"), 0.0),
            activity=str(item.get("activity") or ""),
            teacher_script=str(item.get("teacher_script") or ""),
            intent=str(item.get("intent") or ""),
        )
        for item in (data.get("flow") or [])
        if isinstance(item, dict) and str(item.get("stage") or "").strip()
    ] or f.flow

    # flow 的兜底同理：模型只给补丁、没重发整个数组时，沿用上一版。
    if not lesson.flow and f.flow:
        lesson.flow = f.flow

    # 知识点：优先按 id 对齐原始切分结果，保证时间戳/截图不丢
    by_id = {kp.id: kp for kp in kps}
    ex_lookup = {ex.id: ex for kp in kps for ex in kp.examples}
    order = {kp.id: i for i, kp in enumerate(kps)}

    raw_kps = [k for k in (data.get("knowledge") or []) if isinstance(k, dict)]
    merged: list[KnowledgePoint] = []
    seen: set[str] = set()
    for raw in raw_kps:
        kp_id = str(raw.get("id") or "").strip()
        base = by_id.get(kp_id)
        if base is None:
            base = KnowledgePoint(id=kp_id or f"kp{len(merged) + 1}")
        if base.id in seen:
            continue
        seen.add(base.id)
        prev = prev_kp.get(base.id)
        # 兜底链：新值 → 上一版教案 → 原始切分结果。
        # 少了中间那一步，模型一旦漏写字段就会一路退回到空的原始切分结果，
        # 把 compose 辛苦写出来的定义/公式/解析洗掉。
        base.summary = keep(str(raw.get("summary") or "").strip(),
                            prev.summary if prev else "", base.summary)
        base.definition = keep(str(raw.get("definition") or "").strip(),
                              prev.definition if prev else "", base.definition)
        base.formula_latex = keep(str(raw.get("formula_latex") or "").strip(),
                                  prev.formula_latex if prev else "", base.formula_latex)
        base.key_points = _str_list(raw.get("key_points")) or (prev.key_points if prev else base.key_points)
        base.pitfalls = _str_list(raw.get("pitfalls")) or (prev.pitfalls if prev else base.pitfalls)
        base.teaching_notes = keep(
            str(raw.get("teaching_notes") or "").strip(),
            prev.teaching_notes if prev else "",
            base.teaching_notes,
        )
        base.source_quote = keep(str(raw.get("source_quote") or "").strip(),
                                 prev.source_quote if prev else "", base.source_quote)

        # 这一批模型完全没提到的例题，也要从上一版把内容补回来。
        # 注意不能只判 `not base.examples`：原始切分结果里例题是"有壳没内容"
        # （有 id/title/时间戳，题干答案都是空的），判空会以为已经齐了。
        if prev is not None:
            prev_rows = {e.id: e for e in prev.examples}
            for e in base.examples:
                pe = prev_rows.get(e.id)
                if pe is None:
                    continue
                e.title = e.title or pe.title
                e.statement_latex = e.statement_latex or pe.statement_latex
                e.statement_plain = e.statement_plain or pe.statement_plain
                e.answer = e.answer or pe.answer
                e.solution = e.solution or list(pe.solution)
                e.difficulty = e.difficulty or pe.difficulty
                e.tags = e.tags or list(pe.tags)
                e.notes = e.notes or pe.notes
                e.shot = e.shot or pe.shot
            have = {e.id for e in base.examples}
            for pe in prev.examples:
                if pe.id not in have:
                    base.examples.append(pe.model_copy(deep=True))

        for raw_ex in raw.get("examples") or []:
            if not isinstance(raw_ex, dict):
                continue
            ex_id = str(raw_ex.get("id") or "").strip()
            target = ex_lookup.get(ex_id)
            if target is None:
                target = next((e for e in base.examples if e.title == str(raw_ex.get("title") or "")), None)
            if target is None:
                continue
            # 标题也交给模型订正。原来锁死取切分结果，导致审核报"标题臆造"时
            # 回炉也改不掉——问题在每一轮都被原样带回来，永远收敛不了。
            # 订正完再洗一遍：模型也可能把看图笔记写回标题里。
            new_title = str(raw_ex.get("title") or "").strip()
            if new_title:
                target.title = textnorm.tidy_title(new_title)

            # 例题字段：区分「模型显式给了这个键」和「模型压根没提」。
            # - 显式给了（哪怕是 "" 或 []）：采纳。审核说"这段解析是编造的，删掉"，
            #   模型给 solution: [] 就必须真的删掉，不能被兜底捞回来。
            # - 没提：沿用上一版 → 再沿用原始切分。
            prev_row = prev_ex.get(target.id)
            pv = lambda name, cur: (            # noqa: E731
                getattr(prev_row, name, "") if prev_row is not None else ""
            ) or cur

            def field(name: str):
                given = name in raw_ex and raw_ex.get(name) is not None
                if given:
                    return str(raw_ex.get(name) or "").strip()
                return str(pv(name, getattr(target, name, "")) or "").strip()

            target.statement_latex = field("statement_latex")
            target.statement_plain = field("statement_plain")
            target.answer = field("answer")
            target.difficulty = field("difficulty")
            target.notes = field("notes")

            if "solution" in raw_ex and raw_ex.get("solution") is not None:
                target.solution = _str_list(raw_ex.get("solution"))
            else:
                target.solution = list(pv("solution", []) or [])
            if "tags" in raw_ex and raw_ex.get("tags") is not None:
                target.tags = _str_list(raw_ex.get("tags"))
            else:
                target.tags = list(pv("tags", []) or [])

            shot = str(raw_ex.get("shot") or "").strip()
            if shot:
                target.shot = shot
        merged.append(base)

    # 模型没提到的知识点也要补回来（保证例题不丢）。
    # 优先用上一版教案：原始切分结果现在只有壳，例题是"有 id/title、没内容"的空壳。
    _rest = fallback.knowledge if (fallback and fallback.knowledge) else kps
    for kp in _rest:
        if kp.id not in seen:
            merged.append(kp.model_copy(deep=True))

    merged.sort(key=lambda k: (order.get(k.id, 10_000), k.ts_start))
    lesson.knowledge = merged
    lesson.stats = {
        "knowledge_count": len(merged),
        "example_count": lesson.example_count(),
        "verified_examples": sum(1 for k in merged for e in k.examples if e.verified),
    }
    return lesson


def _str_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [ln.strip(" -•·\t") for ln in value.splitlines() if ln.strip()]
    out = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            text = item.get("text") or item.get("content") or item.get("point")
            if isinstance(text, str) and text.strip():
                out.append(text.strip())
    return out


def _num(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def save(lesson: Lesson, path: Path) -> None:
    path.write_text(lesson.model_dump_json(indent=2), encoding="utf-8")


def load(path: Path) -> Lesson:
    return Lesson(**json.loads(path.read_text(encoding="utf-8")))