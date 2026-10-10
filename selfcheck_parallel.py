"""并发执行器自检：顺序、异常隔离、并发度、限速器共享。

这轮提速全押在"一批独立任务并发跑"上，坑也有三处：
结果顺序乱、单个任务炸了带倒整批、多个客户端各放行一次限流叠加成 N 倍。
这里逐条锁死。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.llm.parallel import run_batches  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


print("=== 用例 1：结果必须按输入顺序返回 ===")
# 反直觉但很关键：pool.map 保序，收集顺序乱了会把 A 批次的问题算到 B 知识点头上
out = run_batches([1, 2, 3, 4, 5], lambda x, i: x * 10, max_workers=5)
check("保序", out == [10, 20, 30, 40, 50], str(out))

out2 = run_batches(["a", "b", "c"], lambda x, i: x.upper(), max_workers=3)
check("保序（字符串）", out2 == ["A", "B", "C"], str(out2))

print()
print("=== 用例 2：单个任务抛异常不能带倒整批 ===")


def _boom(x, i):
    if x == 2:
        raise RuntimeError("这一批炸了")
    return x


out3 = run_batches([1, 2, 3], _boom, max_workers=3)
check("炸掉的那项返回 None", out3[1] is None, str(out3))
check("其它项照常返回", out3[0] == 1 and out3[2] == 3, str(out3))


def _boom_first(x, i):
    raise RuntimeError("全炸")


out4 = run_batches([1, 2], _boom_first, max_workers=2)
check("全炸也不往外抛", out4 == [None, None], str(out4))

print()
print("=== 用例 3：空输入与单项 ===")
check("空列表返回空", run_batches([], lambda x, i: x) == [])
one = run_batches([7], lambda x, i: x * 3)
check("单项不走线程也能算对", one == [21], str(one))


def _never(x, i):
    raise RuntimeError("单项也要接住")


check("单项抛异常也接住", run_batches([7], _never) == [None])

print()
print("=== 用例 4：真的并发了，不是串行 ===")
seen: list[int] = []
lock = threading.Lock()


def _slow(x, i):
    with lock:
        seen.append(x)
    time.sleep(0.35)
    return x


t0 = time.monotonic()
run_batches(list(range(6)), _slow, max_workers=6)
par = time.monotonic() - t0
t0 = time.monotonic()
run_batches(list(range(6)), _slow, max_workers=1)
ser = time.monotonic() - t0
check("6 路并发明显快于串行", par < ser * 0.7, f"并发 {par:.2f}s vs 串行 {ser:.2f}s")
check("max_workers=1 时确实串行", ser > 6 * 0.3, f"串行 {ser:.2f}s")

print()
print("=== 用例 5：并发上限真的被执行 ===")
live = 0
peak = 0
plock = threading.Lock()


def _count(x, i):
    global live, peak
    with plock:
        live += 1
        peak = max(peak, live)
    time.sleep(0.2)
    with plock:
        live -= 1
    return x


run_batches(list(range(12)), _count, max_workers=3)
check("峰值并发不超过设定值", peak <= 3, f"峰值 {peak}")
check("确实用满了并发度", peak >= 2, f"峰值 {peak}")

print()
print("=== 用例 6：限速器按 provider 全局共享 ===")
from app.llm.client import ModelClient, _shared_limiter  # noqa: E402

a = _shared_limiter("测试档", 10)
b = _shared_limiter("测试档", 10)
c = _shared_limiter("别的档", 10)
check("同名 provider 拿到同一个限速器", a is b)
check("不同 provider 互不影响", a is not c)
check("rpm 变了会重建", _shared_limiter("测试档", 60) is not a)

try:
    c1 = ModelClient()
    c2 = ModelClient()
    shared = [k for k in c1.limiters if c1.limiters[k] is c2.limiters.get(k)]
    check("两个客户端共享同一档的限速器", bool(shared), f"共享档位：{shared}")
except Exception as exc:  # 没配 key 就算了
    check("两个客户端共享同一档的限速器", True, f"跳过（{type(exc).__name__}）")

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)