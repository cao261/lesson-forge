"""主备切换自检：错误分类 → 限流器 → 真实降级链路。

不需要真的把主模型打挂，用一个假 Provider 模拟"余额不足"即可。
"""
import time

from app.config import SETTINGS
from app.llm.client import LLMError, ModelClient
from app.llm.routing import RateLimiter, classify_error

print("=" * 66)
print("① 错误分类：哪些该换模型，哪些该重试")
print("=" * 66)
CASES = [
    (429, '{"base_resp":{"status_msg":"insufficient balance, 余额不足"}}', "quota"),
    (400, "Your account balance is not enough", "quota"),
    (402, "quota exhausted", "quota"),
    (401, '{"error":{"message":"Invalid token"}}', "auth"),
    (429, "rate limit exceeded, 1500 rpm", "rate_limit"),
    (429, "请求过于频繁", "rate_limit"),
    (529, "当前服务集群负载较高", "overload"),
    (503, "service unavailable", "overload"),
    (400, "bad request", "other"),
]
ok = True
for status, text, want in CASES:
    got = classify_error(status, text)
    mark = "OK  " if got == want else "FAIL"
    ok &= got == want
    print(f"[{mark}] HTTP {status} {text[:38]:<40} -> {got}（期望 {want}）")
assert ok, "错误分类有偏差"

print()
print("=" * 66)
print("② RPM 限流器")
print("=" * 66)
rl = RateLimiter(rpm=10)
print(f"  10 RPM 最小间隔 = {rl.min_interval:.1f}s")
t0 = time.monotonic()
for i in range(12):
    rl.acquire()
spent = time.monotonic() - t0
# 匀速发：第 1 次不等待，第 2~12 次各等约 6s
print(f"  匀速连发 12 次耗时 = {spent:.1f}s（应 ≈ 11×6 = 66s，且任意两发间隔 ≥6s）")
print(f"  {'OK' if 60 <= spent <= 80 else 'FAIL'}：节奏正确，没有突发")

rl0 = RateLimiter(rpm=0)
t0 = time.monotonic()
for _ in range(50):
    rl0.acquire()
print(f"  不限速(0 RPM) 50 次耗时 = {time.monotonic() - t0:.2f}s → {'OK' if time.monotonic() - t0 < 0.5 else 'FAIL'}")

print()
print("=" * 66)
print("③ 真实降级：主模型报「余额不足」时，自动切到备用")
print("=" * 66)

logs: list[str] = []


class FakeClient(ModelClient):
    """主模型一律报额度不足，备用模型正常返回，用来验证切换路径。"""

    def __init__(self):
        super().__init__(notice=logs.append)
        self.calls: list[str] = []

    def _call(self, provider, messages, **kw):
        self.calls.append(provider["name"])
        if provider["name"].startswith("MiniMax"):
            # 模拟真实返回体
            raise LLMError('[429] {"base_resp":{"status_msg":"insufficient balance"}}')
        self._pending_switch = None
        return '{"ok": true, "from": "fallback"}'


fc = FakeClient()
result = fc.chat_json([{"role": "user", "content": "测试"}])
print(f"  调用顺序     : {' → '.join(fc.calls)}")
print(f"  最终返回     : {result}")
print(f"  当前启用模型 : {fc.active['name']}")
print()
for line in logs:
    print(f"  日志: {line}")

assert result == {"ok": True, "from": "fallback"}, "降级后没有拿到备用模型的结果"
assert fc.active["name"] != "MiniMax-M3", "没有切到备用模型"
assert fc.calls.count("MiniMax-M3") == 1, "额度不足后还在反复打主模型"
print()
print("✅ 主备切换链路正常：主模型只试 1 次就降级，不会空转")

print()
print("=" * 66)
print("④ 切回来：备用也不可用时，是否抛出明确错误")
print("=" * 66)


class AllDead(FakeClient):
    def _call(self, provider, messages, **kw):
        self.calls.append(provider["name"])
        raise LLMError('[401] Invalid token')


ad = AllDead()
try:
    ad.chat_json([{"role": "user", "content": "测试"}])
    print("  FAIL：应该抛错但没抛")
except LLMError as exc:
    print(f"  两档都不可用 → 明确报错：{str(exc)[:90]}")
    print(f"  尝试过的模型: {ad.calls}")

print()
print("=" * 66)
print(f"真实配置：{[p['name'] for p in SETTINGS.active_providers]}")
print("=" * 66)