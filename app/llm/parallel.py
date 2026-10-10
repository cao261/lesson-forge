"""并发跑一批互相独立的大模型任务。

瓶颈不在本地算力，在"一批 LLM 调用一个接一个等"。compose 的例题批次、
审核的知识点批次彼此完全独立，却排成了一条队——实测一轮审核 4 批要
399~689 秒，全花在串行等待上。

三个必须守住的约束：

1. **每个任务各自 new 一个客户端**。客户端里的降级状态（active_index）
   是实例级的，共用一个的话，几个批次同时超时会一起去踩降级逻辑。
2. **限速器全局共享**（见 client._shared_limiter）。否则 N 个客户端各自
   放行 10 RPM，叠加起来超过服务端限流，集体被拒。
3. **并发数要有上限**。并发越高，单个请求越慢、越容易撞超时——那就白并发
   了还多烧一轮重试。默认 3，宁可多跑两轮也别把请求压垮。

回炉（refine）**不走这里**：后一批要读前一批改完的结果，本质上是串行的。
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Sequence, TypeVar

from app.config import SETTINGS

T = TypeVar("T")
R = TypeVar("R")


def run_batches(
    items: Sequence[T],
    worker: Callable[[T, int], R],
    *,
    label: str = "并发",
    on_progress: Callable[[int, int], None] | None = None,
    max_workers: int | None = None,
) -> list[R | None]:
    """并发执行 ``worker(item, index)``，按输入顺序返回结果。

    单个任务抛异常不会拖垮整批——那一项返回 None，由调用方决定怎么办
    （审核那边就是"如实记成这一批失败"，不能假装审过了）。
    """
    n = len(items)
    if n == 0:
        return []
    if n == 1:
        # 单个任务开线程纯属浪费，还有异常传播上的额外麻烦
        try:
            out: list[R | None] = [worker(items[0], 0)]
        except Exception:  # noqa: BLE001
            out = [None]
        if on_progress:
            on_progress(1, 1)
        return out

    want = max_workers or getattr(SETTINGS, "llm_parallel", 6) or 1
    workers = max(1, min(int(want), n))

    results: list[R | None] = [None] * n
    done = [0]
    lock = threading.Lock()

    def _run(idx: int) -> None:
        try:
            results[idx] = worker(items[idx], idx)
        except Exception:  # noqa: BLE001
            results[idx] = None
        finally:
            with lock:
                done[0] += 1
                if on_progress:
                    on_progress(done[0], n)

    if workers == 1:
        for i in range(n):
            _run(i)
        return results

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{label}-") as pool:
        list(pool.map(_run, range(n)))
    return results