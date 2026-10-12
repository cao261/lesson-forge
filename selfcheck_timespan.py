# -*- coding: utf-8 -*-
"""自检：知识点的时间轴必须正确切分、并盖住整段视频。

实测故障（第 6 轮端到端）：360 秒的视频只切出 0-178.5 秒四个板块，
老师 317-358 秒的抢答题整段落在覆盖之外——没有知识点、没有例题、没有截图，
那道题在教案里彻底消失，日志上还看不出少了什么。4 道例题只确认了 1 道。

同一段代码里还有第二个 bug：重叠修补的循环变量 `kp` 是上一个循环泄漏出来的，
永远等于最后一个知识点。结果中间的知识点从来没被切开过（a 0-100 和 b 50-200
原样保留），反倒把最后一个从 300 秒砍到 181 秒。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import KnowledgePoint
from app.stages.normalize import normalize_kps
from app.stages.segment import _normalize, coverage_note

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def kp(kid: str, a: float, b: float) -> KnowledgePoint:
    return KnowledgePoint(id=kid, title=kid, ts_start=a, ts_end=b)


def spans(kps: list[KnowledgePoint]) -> str:
    return " | ".join(f"{k.id} {k.ts_start:g}-{k.ts_end:g}" for k in kps)


# ---------------------------------------------------------------- 重叠必须被切开
over = _normalize([kp("a", 0.0, 100.0), kp("b", 50.0, 200.0), kp("c", 180.0, 300.0)], 300.0)
print("  " + spans(over))
check("重叠的知识点被切开（a 0-100 / b 50-200 → a 0-50）",
      abs(over[0].ts_end - 50.0) < 0.01 and abs(over[1].ts_end - 180.0) < 0.01,
      spans(over))
check("最后一个知识点不被误砍（旧代码把 c 从 300 砍到 181）",
      abs(over[2].ts_end - 300.0) < 0.01, f"c 结束于 {over[2].ts_end:g}")
check("切完之后没有任何重叠",
      all(over[i].ts_end <= over[i + 1].ts_start + 1e-6 for i in range(len(over) - 1)))

# ---------------------------------------------------------------- 必须盖到片尾
tail = _normalize([kp("k1", 0.0, 37.5), kp("k2", 37.5, 99.8),
                   kp("k3", 99.8, 150.3), kp("k4", 150.3, 178.5)], 360.0)
print("  " + spans(tail))
check("最后一个板块一直盖到视频结尾（老师片尾的题不能被丢掉）",
      abs(tail[-1].ts_end - 360.0) < 0.01, f"最后结束于 {tail[-1].ts_end:g}")
check("盖满之后不再报覆盖警告", coverage_note(tail, 360.0) is None,
      str(coverage_note(tail, 360.0)))

gapped = _normalize([kp("g1", 0.0, 40.0), kp("g2", 80.0, 120.0)], 120.0)
print("  " + spans(gapped))
check("中间空洞归前一块，没有 40-80 秒的空档",
      abs(gapped[0].ts_end - 80.0) < 0.01, spans(gapped))

# ---------------------------------------------------------------- 越界与退化输入
oob = _normalize([kp("x", -50.0, 9999.0), kp("y", 10.0, 10.0)], 100.0)
print("  " + spans(oob))
check("负数开始被夹到 0", oob[0].ts_start >= 0.0, spans(oob))
check("超过时长的结束被夹回时长", oob[0].ts_end <= 100.0 + 1e-6, spans(oob))
check("零长板块被撑开成有效区间", oob[1].ts_end > oob[1].ts_start, spans(oob))
check("所有板块都落在 [0, duration] 内",
      all(0.0 <= k.ts_start <= k.ts_end <= 100.0 + 1e-6 for k in oob))

check("空列表安全", _normalize([], 100.0) == [])
check("空标题的板块被丢掉", _normalize([KnowledgePoint(id="e", title="", summary="")], 10.0) == [])
check("duration 为 0 时不越界",
      all(k.ts_start >= 0 for k in _normalize([kp("z", 5.0, 9.0)], 0.0)))

# ---------------------------------------------------------------- 覆盖提醒
check("没盖到片尾要报提醒",
      coverage_note([kp("p", 0.0, 178.5)], 360.0) is not None)
check("提醒里带具体秒数，老师才看得懂",
      "360" in (coverage_note([kp("p", 0.0, 178.5)], 360.0) or ""))
check("盖满了不报提醒", coverage_note([kp("p", 0.0, 360.0)], 360.0) is None)
check("没有知识点时返回 None", coverage_note([], 360.0) is None)

# ---------------------------------------------------------------- 例题时间窗不许倒挂
ex_kps = [KnowledgePoint(id="kp1", title="考点", ts_start=0.0, ts_end=38.0, examples=[])]
from app.models import Example

ex_kps[0].examples.append(Example(id="ex1", title="题", ts_start=38.0, ts_end=158.0))
normalize_kps(ex_kps, 0.0)
ex = ex_kps[0].examples[0]
print(f"  ex1 裁剪后 {ex.ts_start:g}-{ex.ts_end:g}")
check("例题时间窗裁剪后不倒挂（旧代码裁出 38-38）", ex.ts_end > ex.ts_start,
      f"{ex.ts_start:g}-{ex.ts_end:g}")
check("倒挂时兜一个最小 1 秒窗口", ex.ts_end - ex.ts_start >= 1.0,
      f"宽 {ex.ts_end - ex.ts_start:g}")

# 例题落在知识点之后 → 归还给真正覆盖它的那块，而不是压成一个点
orphan = [KnowledgePoint(id="o1", title="考点一", ts_start=0.0, ts_end=38.0, examples=[
    Example(id="oex", title="题", ts_start=40.0, ts_end=70.0)]),
    KnowledgePoint(id="o2", title="考点二", ts_start=38.0, ts_end=200.0, examples=[])]
notes = normalize_kps(orphan, 0.0)
print(f"  归位后 o1={[e.id for e in orphan[0].examples]} o2={[e.id for e in orphan[1].examples]}")
check("落在知识点之外的例题被归还给真正覆盖它的那块",
      [e.id for e in orphan[0].examples] == [] and [e.id for e in orphan[1].examples] == ["oex"],
      f"o1={[e.id for e in orphan[0].examples]} o2={[e.id for e in orphan[1].examples]}")
check("归位有日志，老师看得到例题被挪了", any("已归到" in n for n in notes), str(notes))
moved = orphan[1].examples[0]
check("归还后时间窗完整保留（40-70 不被裁）",
      moved.ts_start == 40.0 and moved.ts_end == 70.0, f"{moved.ts_start:g}-{moved.ts_end:g}")

inside = [KnowledgePoint(id="kp2", title="考点", ts_start=0.0, ts_end=200.0, examples=[
    Example(id="ex2", title="题", ts_start=50.0, ts_end=150.0)])]
normalize_kps(inside, 0.0)
check("本来就在范围内的例题一个数字都不动",
      inside[0].examples[0].ts_start == 50.0 and inside[0].examples[0].ts_end == 150.0)

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails[:6]))
    raise SystemExit(1)
print("✅ 时间轴切分正确，且知识点盖住整段视频")
