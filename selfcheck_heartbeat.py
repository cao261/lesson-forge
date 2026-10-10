"""心跳自检：阻塞等模型时，日志里必须持续出现「还在进行，已等待 X」。

没有心跳的话，一次几分钟的大模型调用期间界面一动不动，
用户分不清是在算还是已经卡死。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# SETTINGS 是导入时从环境变量建的 frozen dataclass，所以必须在导入前设好
os.environ["LLM_HEARTBEAT_SECONDS"] = "3"

from app.config import SETTINGS  # noqa: E402
from app.llm.client import ModelClient, set_notice_hook  # noqa: E402

assert SETTINGS.heartbeat_seconds == 3.0, SETTINGS.heartbeat_seconds

seen: list[str] = []
set_notice_hook(seen.append)

client = ModelClient()

prompt = (
    "请用中文写一份 600 字左右的说明，主题是「因式分解在初中数学中的作用」，"
    "要求分小标题、举两个具体例子。只输出正文，不要 JSON。"
)

print("发起调用（心跳间隔 3 秒）…")
t0 = time.time()
text = client.chat([{"role": "user", "content": prompt}], max_tokens=4000, label="心跳自检")
spent = time.time() - t0

print(f"\n调用完成，用时 {spent:.0f}s，返回 {len(text)} 字\n")
print("----- 期间收到的日志 -----")
for line in seen:
    print("  " + line)

beats = [s for s in seen if "仍在进行" in s]
call_notes = [s for s in seen if s.startswith("→ 调用")]
done_notes = [s for s in seen if s.startswith("✓")]

print()
ok = 0
print(f"  {'✅' if call_notes else '❌'} 调用前有说明（模型 · 用途 · 第几次）")
ok += bool(call_notes)
print(f"  {'✅' if done_notes else '❌'} 调用后有耗时回执")
ok += bool(done_notes)
print(f"  {'✅' if beats else '❌'} 等待期间有心跳（本次 {len(beats)} 条）")
ok += bool(beats)
for b in beats:
    print(f"       {b}")

print(f"\n{'=' * 46}")
print(f"通过 {ok}/3")
print("✅ 进度可见性正常" if ok == 3 else "❌ 进度可见性还缺东西")
sys.exit(0 if ok == 3 else 1)
