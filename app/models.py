"""教案数据结构。整条流水线只认这几个模型，JSON 落盘后可复查、可重跑。"""
from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, Field


def fmt_ts(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class TimeSpan(BaseModel):
    start: float = 0.0
    end: float = 0.0

    def label(self) -> str:
        return f"{fmt_ts(self.start)}–{fmt_ts(self.end)}"


class Cue(BaseModel):
    """字幕里的一条，带时间戳"""

    start: float
    end: float
    text: str


class Example(BaseModel):
    id: str = ""
    title: str = ""
    ts_start: float = 0.0
    ts_end: float = 0.0
    statement_latex: str = Field(default="", description="题干，含公式，LaTeX，行内用 $...$")
    statement_plain: str = Field(default="", description="题干的纯文字版本，不含公式符号")
    answer: str = ""
    solution: list[str] = Field(default_factory=list, description="分步解析")
    difficulty: str = "中等"
    tags: list[str] = Field(default_factory=list, description="考查的知识点")
    shot: str | None = Field(default=None, description="题目截图文件名")
    shot_confidence: float = 0.0
    board_shot: str | None = Field(default=None, description="配套板书截图文件名")
    verified: bool = Field(default=False, description="多模态模型已确认截图里确有完整题目")
    # 老师是口述讲解、没有出可做的题。这类"例题"曾经照常出现在学生的
    # 「即学即练」里，只有一个标题和一个空白答题区——学生拿到手完全没法用。
    # 判定后把它们降级成口述要点，不再冒充例题。
    is_exercise: bool = Field(default=True, description="是否真的有一道可做的题")
    notes: str = ""


class KnowledgePoint(BaseModel):
    id: str = ""
    title: str = ""
    ts_start: float = 0.0
    ts_end: float = 0.0
    summary: str = Field(default="", description="讲了什么，来自字幕")
    definition: str = Field(default="", description="定义、定理、结论的规范表述")
    formula_latex: str = Field(default="", description="核心公式，LaTeX")
    key_points: list[str] = Field(default_factory=list, description="必须掌握的要点")
    pitfalls: list[str] = Field(default_factory=list, description="易错点与注意事项")
    teaching_notes: str = Field(default="", description="给家教的讲法建议")
    source_quote: str = Field(default="", description="字幕原文依据，用于核查幻觉")
    board_shot: str | None = Field(default=None, description="知识点板书总结截图文件名")
    examples: list[Example] = Field(default_factory=list)


class FlowStep(BaseModel):
    stage: str = ""
    duration_min: float = 0.0
    activity: str = ""
    teacher_script: str = ""
    intent: str = ""


class ReviewIssue(BaseModel):
    # fact = 与视频对不上的事实错误（编造）；supplement = 老师自己会补的教学内容。
    # 这两类以前混在一起报，导致口诀/作业/一般公式全被判成"编造"，
    # 每份教案都判 revise、两轮回炉也收敛不了。
    kind: Literal["fact", "supplement"] = "fact"
    severity: Literal["blocker", "major", "minor"] = "major"
    target: str = ""
    problem: str = ""
    suggestion: str = ""

    @property
    def is_fact(self) -> bool:
        return self.kind != "supplement"


class ReviewReport(BaseModel):
    verdict: Literal["pass", "revise", "fail"] = "revise"
    score: float = 0.0
    rounds: int = 0
    summary: str = ""
    issues: list[ReviewIssue] = Field(default_factory=list)
    rule_checks: list[str] = Field(default_factory=list)
    # 一句话统计：事实错误几条、教学补充几条。给老师一眼看"这稿子脏不脏"。
    stats_note: str = ""
    hallucination_risk: str = ""
    teacher_notes: str = ""
    # 模型交叉审核是否真的跑完了。False = 只过了本地规则体检，
    # 编造风险未经核查，绝不能当成"已审核通过"呈现给老师。
    model_review_ok: bool = False


class LessonMeta(BaseModel):
    title: str = ""
    subject: str = "数学"
    grade: str = ""
    teacher: str = ""
    student: str = ""
    duration_min: float = 0.0
    source_url: str = ""
    source_title: str = ""
    uploader: str = ""
    video_duration: float = 0.0
    generated_at: str = ""
    generator: str = "LessonForge · MiniMax-M3"


class Lesson(BaseModel):
    meta: LessonMeta = Field(default_factory=LessonMeta)
    objectives: list[str] = Field(default_factory=list, description="教学目标")
    key_points: list[str] = Field(default_factory=list, description="教学重点")
    difficulties: list[str] = Field(default_factory=list, description="教学难点")
    prerequisites: list[str] = Field(default_factory=list, description="前置知识")
    materials: list[str] = Field(default_factory=list, description="课前准备与教具")
    knowledge: list[KnowledgePoint] = Field(default_factory=list)
    flow: list[FlowStep] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list, description="易错点汇总")
    homework: list[str] = Field(default_factory=list)
    review: ReviewReport = Field(default_factory=ReviewReport)
    stats: dict[str, Any] = Field(default_factory=dict)

    def example_count(self) -> int:
        """真正的例题数（不含口述要点）。统计口径要和 Word 里印出来的一致。"""
        return sum(1 for kp in self.knowledge for ex in kp.examples if ex.is_exercise)

    def talk_count(self) -> int:
        """被判为"老师只是口述、没出题"的条目数。"""
        return sum(1 for kp in self.knowledge for ex in kp.examples if not ex.is_exercise)

    def touch(self) -> None:
        self.meta.generated_at = time.strftime("%Y-%m-%d %H:%M:%S")