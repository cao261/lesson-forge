"""视觉链路端到端自检：不跑整条流水线，直接验证"看图→转写 JSON"这条路通。"""
from pathlib import Path

from app.llm.client import LLMError, ModelClient
from app.llm.prompts import TRANSCRIBE_USER
from app.stages.vision import ACCEPT_CONFIDENCE, prepare_frame

SHOT = Path("workspace/332db78e95/shots/ex1_c1.jpg")

print("=" * 70)
print("① 签名修复验证：vision_json 不再缺参数")
print("=" * 70)
client = ModelClient()
prompt = TRANSCRIBE_USER.format(
    title="2027高考数学必刷100讲", ts="01:06",
    kp_title="例题：必要不充分条件求参数",
    context="[59s] 它说x在-2到m之间 / [62s] 是右边这个二次不等式小于0的必要不充分条件 / [65s] 则m的取值范围是多少",
)
try:
    data = client.vision_json(prompt, [prepare_frame(SHOT)],
                              temperature=0.0, max_tokens=16000, label="transcribe")
    print(f"  调用成功，返回类型：{type(data).__name__}")
except LLMError as exc:
    print(f"  失败：{str(exc)[:300]}")
    raise SystemExit(1)

print()
print("=" * 70)
print("② 模型实际读出了什么（和截图真值比对）")
print("=" * 70)
print(f"  has_problem = {data.get('has_problem')}")
print(f"  confidence  = {data.get('confidence')}")
print(f"  题干        = {str(data.get('statement_latex'))[:200]}")

TRUTH = "-2 < x < m"          # 截图上就是没有负号
stmt = str(data.get("statement_latex") or "")
normalized = stmt.replace(" ", "")
has_truth = TRUTH.replace(" ", "") in normalized
wrong_variant = "-2<x<-m" in normalized.replace("$", "").replace("\\", "")

print()
print(f"  真值含 {TRUTH!r}：{'✅ 正确' if has_truth else '❌ 没读到'}")
print(f"  误读成 -2<x<-m ：{'❌ 是（这正是上次出错的地方）' if wrong_variant else '✅ 没有'}")

print()
print(f"  通过判定阈值 {ACCEPT_CONFIDENCE}："
      f"{'✅' if data.get('has_problem') and float(data.get('confidence', 0)) >= ACCEPT_CONFIDENCE else '❌'}")

print()
print("=" * 70)
print("③ 纯调用健壮性：给一个空白图，应该回答『没有题目』而不是报错")
print("=" * 70)
from PIL import Image
blank = Path("selfcheck_out/blank.jpg")
Image.new("RGB", (1280, 720), (255, 255, 255)).save(blank, quality=90)
try:
    d2 = client.vision_json(prompt, [prepare_frame(blank)], temperature=0.0,
                            max_tokens=16000, label="transcribe")
    print(f"  空白图 -> has_problem={d2.get('has_problem')}（期望 False，且不报错）")
except Exception as exc:
    print(f"  ❌ 空白图导致异常：{str(exc)[:200]}")