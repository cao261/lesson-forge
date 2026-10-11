"""切分之后的结构体检。

这一层以前没有，于是**结构性问题一路带到最后，回炉又修不了**——
审核每轮都报"例3 和例2 重复""时间戳重叠"，模型只能反复改文字，
两轮回炉下来问题一条不少。这就是"跑很久还抽卡"的真正来源。

这里在切分刚结束时把能确定的问题就地处理掉：

1. **裁时间**：例题的时间必须落在它所属知识点的时间段内
2. **去重复**：题干高度相似 + 时间段大面积重叠 → 是同一道题被切了两次，删一道
3. **补重叠**：同一个知识点里两道例题时间段大面积重叠 → 至少把后者推开

只动时间戳和"删掉明显重复的那道题"，不碰题干/答案/解析的文字。
判不准的一律不动——宁可留着给审核报出来，也不替用户删内容。
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.models import KnowledgePoint

# 题干相似度到这个程度就报一句"疑似同一道题"（只报，不删，见 normalize_kps）
DUP_RATIO = 0.82
# 时间重叠占"较短那道题"的比例超过这个值，才算真的重复。
# 0.35 是照着真实数据定的：因式分解那节课里 ex4/ex5 是同一道三小题，
# 相似度 0.95 但重叠 44%，卡在 0.45 门槛外就漏掉了。
DUP_OVERLAP = 0.35
# 包含关系（整题 vs 它的一道小题）+ 时间挨得近 → 只报，不删。
# 这类删不得：老师完全可能先讲整题、再单独讲其中一问。
DUP_GAP = 30.0
# 同一知识点内两道题的重叠警戒线（只提示，不自动改）
WARN_OVERLAP = 0.35

# `$` 必须在噪声表里。以前漏了它，`$\quad$` 归一化后剩下 `$$`，
# 于是两道一字不差的题（只是 \quad 和 \qquad 的写法不同）相似度只有 0.816，
# 卡在 0.82 门槛下面——同一道题切了两次反而检测不出来。
#
# 而运算符 `= + - * / |` **绝不能**进噪声表。以前它们在里面，于是
# `x^2-2x+3=0` 和 `x^2-2x-3=0` 归一化后都变成 `x^22x30`，相似度 1.000，
# 两道不同的题被判成重复**直接删掉**，日志里只留一行。
# 运算符是题目的语义，剥掉它就等于抹掉题目本身。这里只剥排版噪声。
_NOISE = re.compile(r"[\s$，,。.；;：:、！!？?（）()【】\[\]「」『』“”\"'`~—_\\]+")


def _norm(s: str) -> str:
    """把题干压成可比的形式：去掉标点空白，LaTeX 指令只留主体。"""
    s = re.sub(r"\\[a-zA-Z]+", "", s or "")
    s = _NOISE.sub("", s)
    return s.lower()


def _span(ex) -> tuple[float, float]:
    lo, hi = float(ex.ts_start or 0.0), float(ex.ts_end or 0.0)
    return (lo, hi) if hi > lo else (lo, lo + 1.0)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    inter = min(a[1], b[1]) - max(a[0], b[0])
    if inter <= 0:
        return 0.0
    shorter = min(a[1] - a[0], b[1] - b[0])
    return inter / shorter if shorter > 0 else 0.0


def _gap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return max(a[0], b[0]) - min(a[1], b[1])


def suspicious_pairs(kps: list[KnowledgePoint]) -> list[str]:
    """只报不删的重复嫌疑：一道题的题干是另一道的子串，且时间挨得近。

    真实案例：ex2 是「(1)√(5-2√6) (2)√(4-√15)」整题，ex3 是其中的第 (2) 小题。
    这算不算重复要看老师怎么讲，所以只提醒，不替用户删。
    """
    out: list[str] = []
    for kp in kps:
        for i in range(len(kp.examples)):
            for j in range(i + 1, len(kp.examples)):
                a, b = kp.examples[i], kp.examples[j]
                ta = _norm(a.statement_latex) or _norm(a.statement_plain)
                tb = _norm(b.statement_latex) or _norm(b.statement_plain)
                if len(ta) < 6 or len(tb) < 6:
                    continue
                short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
                if short not in long_:
                    continue
                sa, sb = _span(a), _span(b)
                if _overlap(sa, sb) < DUP_OVERLAP and _gap(sa, sb) > DUP_GAP:
                    continue
                out.append(
                    f"[{kp.id}] {a.id} 与 {b.id} 题干存在包含关系且时间相邻，"
                    f"疑似同一道题被切成了两道（{a.id}: {_norm(a.statement_latex)[:24]}… / "
                    f"{b.id}: {_norm(b.statement_latex)[:24]}…），请人工确认"
                )
    return out


# 「这不是题干，是对画面的描述」的典型句式。
# 实测踩过：概念讲解页被模型当成题，statement_latex 写成
# 「\text{通过图形展示倾斜角与斜率不是一一对应的关系}」——
# 这不是学生能做的题，是一句图注，混进例题里就变成了空壳。
#
# 注意不能单看这些词：「如图 / 见图 / 图上」在真题里非常常见
# （"如图，直线 l 过点 A(1,2)，求斜率 k"），光凭它们判会把真题误杀。
# 所以必须同时满足"像图注"且"没有设问"两个条件才算数。
_DESCRIBE = re.compile(
    r"(通过图(形|示)|图(形|示)(展示|说明|体现|呈现|反映)"
    r"|展示了|体现了|呈现了|反映了|该图(展示|说明|体现|呈现|反映)"
    r"|画面(中|上|里)|课件(中|上))")

# 学生在题上会被要求做的事。有它就说明这确实是一道题。
_ASK = re.compile(
    r"(求|证明|证出|判断|是否|为什么|理由|计算|解出|填空|选择|等于多少"
    r"|是多少|怎么做|如何求|多少)")


def _looks_like_statement(text: str) -> bool:
    """这段文字到底是不是一道学生能做的题。"""
    s = (text or "").strip()
    if len(_norm(s)) < 6:
        return False
    if _DESCRIBE.search(s) and not _ASK.search(s):
        return False        # 整句都在描述画面，且没让学生做任何事 → 图注
    return True


def classify_exercises(kps: list[KnowledgePoint]) -> list[str]:
    """分清"真例题"和"老师只是口述讲解"，返回人话清单。

    为什么必须在 compose 之前判：切分阶段为了"题目全覆盖"，会把每一段概念
    讲解都立成一条例题（标题就是「概念辨析·斜面向上方向」「物理类比·斜面坡度」）。
    看图阶段又确认画面里根本没有题面，于是题干是空的。等到出 Word 时，
    学生的「即学即练」里就只剩一个标题加一块空白答题区——完全没法用。

    判据（三条全中才算口述要点，缺一不可）：
      1. 没有题干：`statement_latex` 和 `statement_plain` 都是空的；
      2. 视觉也没确认到题面：`verified` 为假；
      3. 或者题干其实是对画面的描述（"通过图形展示…"），不是学生能做的题。
    只要题干是真的题目，就当老师真的出了一道题，哪怕图没截到。
    """
    notes: list[str] = []
    for kp in kps:
        for ex in kp.examples:
            text = (ex.statement_latex or ex.statement_plain or "").strip()
            has_text = bool(_looks_like_statement(text))
            if has_text or ex.verified:
                ex.is_exercise = True
                continue
            ex.is_exercise = False
            ex.statement_latex = ""        # 图注不是题干，别再往下游传
            ex.statement_plain = ""
            ex.shot = None                 # 没题干就别再挂着截图名，避免下游误判
            ex.board_shot = None
            why = "没有题干、画面里也没有题面" if not text else "题干只是对画面的描述，不是可做的题"
            notes.append(
                f"[{kp.id}/{ex.id}]「{ex.title}」{why}"
                f"——按口述要点处理，不进学生的即学即练"
            )
    return notes


def normalize_kps(kps: list[KnowledgePoint], duration: float = 0.0) -> list[str]:
    """就地修结构问题，返回给日志看的人话清单。"""
    notes: list[str] = []

    for kp in kps:
        # ---- 1. 例题时间裁进知识点范围内 ----
        for ex in kp.examples:
            before = (ex.ts_start, ex.ts_end)
            if kp.ts_end > kp.ts_start:
                ex.ts_start = max(ex.ts_start, kp.ts_start)
                ex.ts_end = min(ex.ts_end, kp.ts_end) if ex.ts_end > ex.ts_start else ex.ts_start + 1.0
            if duration > 0 and ex.ts_end > duration:
                ex.ts_end = max(ex.ts_start + 1.0, duration)
            if (ex.ts_start, ex.ts_end) != before:
                notes.append(
                    f"[{kp.id}/{ex.id}] 例题时间越出知识点范围，"
                    f"{before[0]:.0f}–{before[1]:.0f}s → {ex.ts_start:.0f}–{ex.ts_end:.0f}s"
                )

        # ---- 2. 去重：题干一模一样 + 时间大幅重叠 = 同一道题切了两次 ----
        # 只有归一化后**完全相同**才敢删。以前是相似度 ≥0.82 就删，
        # 配合把运算符当噪声剥掉的 _norm，能把两道不同的题判成重复删掉
        # （见 _NOISE 处的实测）。像"只差 \quad 和 \qquad 写法"这种真重复，
        # _norm 之后本来就会变成同一个串，所以放宽到完全相同并不会漏。
        # 剩下的"很像但不完全一样"一律只报不删，和 suspicious_pairs 一个口径。
        keep: list = []
        for ex in kp.examples:
            text = _norm(ex.statement_latex) or _norm(ex.statement_plain) or _norm(ex.title)
            span = _span(ex)
            twin = None
            for kept in keep:
                ktext = _norm(kept.statement_latex) or _norm(kept.statement_plain) or _norm(kept.title)
                if not text or not ktext:
                    continue
                if text == ktext:
                    twin = kept
                    break
            if twin is None:
                # 不完全相同但很像的，留着并报一句，交给审核/老师判断
                for kept in keep:
                    ktext = _norm(kept.statement_latex) or _norm(kept.statement_plain) or _norm(kept.title)
                    if text and ktext and SequenceMatcher(None, text, ktext).ratio() >= DUP_RATIO:
                        notes.append(
                            f"[{kp.id}] {kept.id} 与 {ex.id} 题干高度相似但不完全相同"
                            f"（{text[:20]}…），已一并保留，请人工确认"
                        )
                        break
                keep.append(ex)
            elif _overlap(span, _span(twin)) >= DUP_OVERLAP:
                notes.append(
                    f"[{kp.id}] 删掉重复例题 {ex.id}（与 {twin.id} 题干完全一致且时间重叠）"
                )
            else:
                # 题干一模一样但时间离得远：老师可能在课尾把同一道题又讲了一遍，
                # 也可能是两道巧合的题，不删。
                keep.append(ex)
        kp.examples = keep

        # ---- 3. 同知识点内例题时间重叠：只提示，不自动改 ----
        for i in range(len(kp.examples)):
            for j in range(i + 1, len(kp.examples)):
                a, b = kp.examples[i], kp.examples[j]
                ov = _overlap(_span(a), _span(b))
                if ov >= WARN_OVERLAP:
                    notes.append(
                        f"[{kp.id}] {a.id} 与 {b.id} 时间重叠 {ov:.0%}"
                        f"（{a.ts_start:.0f}–{a.ts_end:.0f}s vs {b.ts_start:.0f}–{b.ts_end:.0f}s）"
                    )
    return notes