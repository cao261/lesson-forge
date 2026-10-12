# -*- coding: utf-8 -*-
"""自检：课尾题按片尾重搜，不信切分模型给的 ts_start。

实测故障（第 8 轮）：两道抢答题的真实时间是 05:17 和 05:46，切分模型给的
却是 04:05 和 04:18——偏了 65 秒。抽帧照着错时间点去抽，抽到的画面里当然
没有那道题，看图确认两两失败，两道题全被降级成口述要点，教案里一道例题
都不剩。而日志上只写着"没有题面"，完全看不出是这个原因。

这两道题有个稳定特征：时间窗一直拖到视频结束（ts_end == duration）。
课尾抢答题就是这个定义，中间的例题不会一路拖到片尾。用它当判据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import SETTINGS
from app.models import Example, KnowledgePoint
from app.stages.shots import TAIL_SHOT_WINDOW

fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def window(ex: Example, duration: float) -> tuple[float, float]:
    """复刻 capture_examples 里的取窗逻辑（改了这里也要改这里）。"""
    if duration > 0 and ex.ts_end >= duration - 2.0:
        return max(0.0, duration - TAIL_SHOT_WINDOW), duration
    lo = max(0.0, ex.ts_start - SETTINGS.shot_pre_roll)
    hi = min(duration, max(ex.ts_start + 12.0, min(ex.ts_start + 25.0, ex.ts_end + 5.0)))
    if hi - lo < 3:
        hi = min(duration, lo + 3)
    return lo, hi


DUR = 360.0

# ---------------------------------------------------------------- 第 8 轮的真实故障
TAIL = [
    # (模型给的起点, 模型给的终点, 题目真实时间)
    (245.0, 360.0, 317.0),   # ex3 被给成 04:05，真实 05:17
    (258.0, 360.0, 346.0),   # ex4 被给成 04:18，真实 05:46
]
for start, end, real in TAIL:
    lo, hi = window(Example(id="ex", title="t", ts_start=start, ts_end=end), DUR)
    check(f"课尾题偏早 65 秒也能搜到（真实 {real:.0f}s）", lo <= real <= hi,
          f"搜索窗 {lo:.0f}–{hi:.0f}s，真实 {real:.0f}s")

# 第 7 轮那组本来就对，重搜后也要仍然覆盖
for start, end, real in [(309.0, 360.0, 317.0), (340.0, 360.0, 346.0)]:
    lo, hi = window(Example(id="ex", title="t", ts_start=start, ts_end=end), DUR)
    check(f"本来就标对的课尾题仍然覆盖（真实 {real:.0f}s）", lo <= real <= hi,
          f"搜索窗 {lo:.0f}–{hi:.0f}s")

# ---------------------------------------------------------------- 中间的例题不受影响
MID = [
    (46.0, 73.0),
    (206.0, 245.0),
    (150.0, 165.0),
]
for start, end in MID:
    ex = Example(id="ex", title="t", ts_start=start, ts_end=end)
    lo, hi = window(ex, DUR)
    check(f"中间例题仍按 ts_start 取窗：{start:.0f}–{end:.0f}s", lo <= start <= hi,
          f"搜索窗 {lo:.0f}–{hi:.0f}s")
    check(f"中间例题没被拖到片尾：{start:.0f}s", hi < DUR - TAIL_SHOT_WINDOW + 5,
          f"窗口右端 {hi:.0f}s")

# 常规例题的窗口仍然是"题目刚引入"那一段，不能被课尾逻辑污染
one = Example(id="ex", title="t", ts_start=100.0, ts_end=130.0)
lo, hi = window(one, DUR)
check("常规例题从 ts_start 之前一点点开始", lo == 100.0 - SETTINGS.shot_pre_roll,
      f"lo={lo:.0f}")
check("常规例题右端不超过 ts_start+25", hi <= 100.0 + 25.0 + 1e-6, f"hi={hi:.0f}")

# ---------------------------------------------------------------- 边界
check("视频很短时课尾窗不越界",
      window(Example(id="ex", title="t", ts_start=10.0, ts_end=20.0), 20.0) == (0.0, 20.0),
      str(window(Example(id="ex", title="t", ts_start=10.0, ts_end=20.0), 20.0)))
# duration 未知时不能盲目当成"课尾题"——那会把整个搜索窗拉到 0，
# 明明有 ts_start 可用反而什么都不搜。判据里必须有 duration > 0。
_lo0, _hi0 = window(Example(id="ex", title="t", ts_start=300.0, ts_end=360.0), 0.0)
check("duration=0 时不触发课尾分支（仍按 ts_start 取窗）",
      _lo0 == 300.0 - SETTINGS.shot_pre_roll, f"lo={_lo0:.0f}, hi={_hi0:.0f}")
check("差一点没到片尾（差 3 秒）仍按原逻辑",
      window(Example(id="ex", title="t", ts_start=300.0, ts_end=357.0), DUR)[1] < 360.0,
      str(window(Example(id="ex", title="t", ts_start=300.0, ts_end=357.0), DUR)))
check("重搜窗口不是无限大（别把整节课都捞进来）",
      20.0 <= TAIL_SHOT_WINDOW <= 90.0, f"{TAIL_SHOT_WINDOW:.0f} 秒")

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails[:6]))
    raise SystemExit(1)
print("✅ 课尾题不管模型把时间标偏多少，都能搜到真实画面")
