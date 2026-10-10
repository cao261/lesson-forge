"""标题落地性检查自检：字幕里没有的括注标签必须被判为臆造，真的不能误伤。"""
from app.models import Cue
from app.stages.review import _ungrounded_labels, _norm

# 造一份字幕：故意含 ASR 噪声 "F2"，不含"例2""变式"
CUES = [
    Cue(start=0.0, end=3.0, text="今天讲充分条件和必要条件"),
    Cue(start=3.0, end=6.0, text="跟X之后交易两个点，一个点是一个F2"),
    Cue(start=6.0, end=9.0, text="另一个点刚好就是等于3"),
    Cue(start=9.0, end=12.0, text="那么我们来看一道例题"),
]
TR = _norm("".join(c.text for c in CUES))

CASES = [
    ("① 字幕里没有的标签要抓出来",
     "例题：必要不充分条件求参数（例2 / 变式）·求参数范围",
     ["（例2 / 变式）"],
     "这段就是实际出事的那条标题"),

    ("② 字幕里出现过的标签不能误伤",
     "本课变式训练：充分条件",
     [],
     "标题里的「变式」字幕确实讲了"),

    ("③ 空括号/超长不判",
     "求参数（）（：（这是一段非常非常长的超过二十个字的括注内容用来测试长度上限）",
     [],
     "空括号忽略，超长忽略"),

    ("④ 没有括号的标题不判",
     "必要不充分条件的判断方法",
     [],
     "普通标题不该被碰"),

    ("⑤ 中文方括号 / 方头括号也能抓",
     "充分条件【例2】讲解",
     ["【例2】"],
     "不同括号形式都要覆盖"),

    ("⑥ 字幕为空时不误判（拿不到字幕就放弃）",
     "任意（什么标签都行）",
     [],
     "没有字幕依据时不该乱报"),
]

print("=" * 72)
print("标题落地性检查自检")
print("=" * 72)
ok = 0
for name, title, expect, why in CASES:
    got = _ungrounded_labels(title, TR if "字幕为空" not in name else "")
    good = got == expect
    print(f"  {'✅' if good else '❌'} {name}")
    print(f"       命中 {got}（期望 {expect}）— {why}")
    ok += good

print("-" * 72)
print(f"  {ok}/{len(CASES)} 通过")

# 端到端：构造一份带幻觉标题的教案，确认规则体检能抓到并判成 major
# （不依赖 workspace 里的真实数据——那个每跑一次就变，自检必须自带样本）
print()
print("=" * 72)
print("接入验证：规则体检能否抓到幻觉标题")
print("=" * 72)
import json
from pathlib import Path

from app.models import Example, KnowledgePoint, Lesson, LessonMeta
from app.stages.review import _rule_issue, rule_check

WS = Path("workspace/332db78e95")
tr = json.loads((WS / "transcript.json").read_text(encoding="utf-8"))
cues = [Cue.model_validate(c) for c in (tr["cues"] if isinstance(tr, dict) else tr)]

lesson = Lesson(
    meta=LessonMeta(title="自检样本", video_duration=180.0),
    knowledge=[KnowledgePoint(
        id="kp1", title="判断充分必要条件的方法", ts_start=0.0, ts_end=170.0,
        examples=[Example(
            id="ex1", title="例题：必要不充分条件求参数（例2 / 变式）·求参数范围",
            ts_start=60.0, ts_end=170.0,
            statement_latex="若 $-2<x<m$ 是 $x^2-x-6<0$ 的必要不充分条件",
            answer="$m\\in(3,+\\infty)$", solution=["第一步", "第二步"],
            shot="ex1_c1.jpg", verified=True,
        )],
    )],
)

problems = rule_check(lesson, WS / "shots", 180.0, cues)
hits = [p for p in problems if "字幕里没有逐字出现" in p]
for p in problems:
    mark = "🎯" if p in hits else "  "
    print(f"  {mark} {p}")

print()
for p in hits:
    issue = _rule_issue(p)
    print(f"  → 严重级别：{issue.severity}（必须是 major 才会回炉）")
    print(f"  → 修订建议：{issue.suggestion}")

print()
if hits and _rule_issue(hits[0]).severity == "major":
    print("  ✅ 幻觉标题被抓住，且被判为 major，会被回炉修掉")
else:
    print("  ❌ 没抓到，或级别不是 major")