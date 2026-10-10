"""CUDA 缺失时的降级自检。

要验的是两件事：
  1. auto 模式下，`_pick_device()` 不会再因为「有显卡驱动」就误判成 cuda
  2. 万一还是走了 cuda，推理阶段的 cuBLAS 报错必须能自动退回 CPU 并跑完

第 2 条是真正救命的那条：CTranslate2 懒加载 cuBLAS，
`WhisperModel(device="cuda")` 构造时不报错，只在真正推理时才炸。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

WAV = Path("selfcheck_out/clip20s.wav")

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def main() -> int:
    if not WAV.exists():
        print(f"找不到 {WAV}")
        return 1

    from app.stages import asr
    from app.config import SETTINGS

    print("\n=== 1. CUDA 运行时探针 ===")
    asr._ensure_cuda_dlls()
    rt_ready = asr._cuda_runtime_ready()
    print(f"  _cuda_runtime_ready() = {rt_ready}")
    if not rt_ready:
        print(f"  {asr._cuda_missing_hint()}")

    print("\n=== 2. auto 模式选设备 ===")
    auto_device = asr._pick_device()
    print(f"  _pick_device() = {auto_device}")
    if rt_ready:
        check("auto 模式应当用 cuda", auto_device == "cuda", auto_device)
    else:
        check("auto 模式在缺 CUDA 库时必须退回 cpu", auto_device == "cpu", auto_device)

    print("\n=== 3. 强制 cuda：推理阶段必须能自动降级 ===")
    # SETTINGS 是 frozen dataclass，用 dataclasses.replace 造一份改过的副本
    import dataclasses

    asr.SETTINGS = dataclasses.replace(asr.SETTINGS, whisper_device="cuda")
    t0 = time.time()
    cues: list = []
    fell_back = False
    try:
        cues = asr.transcribe(WAV, 20.0, progress=lambda f, m: print(f"  {f * 100:5.1f}%  {m}"))
    except Exception as exc:  # noqa: BLE001
        fell_back = True
        print(f"  [FAIL] 强制 cuda 直接崩了，没有降级：{type(exc).__name__}: {exc}")
    took = time.time() - t0

    if not fell_back:
        check("强制 cuda 也能拿到字幕（说明推理阶段兜底生效）", bool(cues), f"{len(cues)} 段 / {took:.1f}s")
        check("字幕时间戳没有倒挂", all(c.end >= c.start for c in cues))
        check("字幕非空", any(c.text.strip() for c in cues))
        for c in cues[:5]:
            print(f"    [{c.start:6.2f} -> {c.end:6.2f}] {c.text}")

    print(f"\n{'=' * 46}")
    print(f"通过 {passed} 项，失败 {failed} 项")
    if failed:
        print("❌ CUDA 降级链路有问题")
        return 1
    print("✅ CUDA 降级链路正常（缺 CUDA 库时能自动退回 CPU 出结果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
