"""多模型主备路由。

默认主模型用 MiniMax-M3；一旦它额度用完，自动切到备用模型继续跑，
不会让整条流水线因为"余额不足"直接失败。

备用模型通常是限速的免费额度，所以每档都带一个 RPM 限流器。
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """匀速限流器：保证任意两次调用之间至少间隔 60/rpm 秒。

    刻意不做"突发桶"（比如 10 RPM 就先连发 10 次再等一分钟）：
    那种打法平均速率虽然达标，但会瞬间打满服务端的滑窗限流而被拒。
    匀速发反而更稳，也更好估算整轮要等多久。
    """

    def __init__(self, rpm: int) -> None:
        self.rpm = max(0, int(rpm))
        self.min_interval = 60.0 / self.rpm if self.rpm else 0.0
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """阻塞到允许发起下一次调用为止，返回本次等待秒数。"""
        if not self.rpm:
            return 0.0
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_allowed)
            self._next_allowed = slot + self.min_interval
            wait = slot - now
        if wait > 0:
            time.sleep(wait)
        return wait


# ------------------------------------------------------------------ 错误分类
QUOTA_HINTS = (
    "insufficient", "余额", "欠费", "arrears", "quota", "balance", "credit",
    "额度", "exhaust", "no quota", "out of credit", "arrearage", "payment",
    "充值", "billing", "账单",
)
RATE_HINTS = (
    "rate limit", "ratelimit", "too many requests", "rpm", "tpm",
    "请求过于频繁", "限流", "qps",
)
AUTH_HINTS = ("invalid token", "unauthorized", "forbidden", "token not provided",
              "invalid api key", "authentication", "401", "403", "权限")
OVERLOAD_HINTS = ("overloaded", "529", "负载", "busy", "service unavailable",
                  "timeout", "gateway")


def classify_error(status: int, text: str) -> str:
    """把一次失败的调用归类，供路由决定"重试 / 换模型 / 直接放弃"。

    返回值：quota | rate_limit | auth | overload | other
    """
    low = (text or "").lower()

    # 额度问题优先于限流：两者常常都是 429，但处理方式完全不同
    if any(h in low for h in QUOTA_HINTS):
        return "quota"
    if any(h in low for h in AUTH_HINTS):
        return "auth"
    if any(h in low for h in RATE_HINTS):
        return "rate_limit"
    if status in {429, 529} or any(h in low for h in OVERLOAD_HINTS):
        return "overload" if status != 429 else "rate_limit"
    if 500 <= status < 600:
        return "overload"
    return "other"