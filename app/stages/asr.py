"""阶段 2：本地 Whisper 转写，产出带时间戳的字幕。

用 faster-whisper（CTranslate2）。有 N 卡就走 GPU，没有则自动退回 CPU。
词级时间戳（word_timestamps）是为了后面把例题定位到秒。
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
from pathlib import Path
from typing import Callable

from app.config import SETTINGS
from app.models import Cue

Progress = Callable[[float, str], None]

_MODEL_CACHE: dict = {}
# os.add_dll_directory() 返回的句柄一旦被回收，目录就从搜索路径里消失了，
# 所以必须一直攥着；预加载的 DLL 句柄同理（进程内复用已加载模块）。
_DLL_HANDLES: list = []

# 按名字加载不起来的 DLL，先按全路径预加载进进程。
# 原因：Windows 用 LoadLibrary("x.dll") 这种裸名字加载时，
# 不会去「被加载 DLL 所在的目录」里找它的依赖，AddDllDirectory 对依赖解析不生效。
# 先把依赖钉进模块表，之后裸名字的加载就能直接命中。
_PRELOAD_LIBS = (
    "cublasLt64_12.dll",   # cublas64_12.dll 依赖它
    "nvblas64_12.dll",
    "cublas64_12.dll",
    "cudart64_12.dll",
    "cudnn64_9.dll",
)


def _patch_av_open() -> None:
    """兼容 PyAV 14+。

    faster-whisper 仍按旧签名调用 av.open(..., metadata_errors="ignore")，
    而新版 PyAV 已经移除了这个参数，不打补丁会直接 TypeError 起不来。
    """
    try:
        import av
    except ImportError:  # pragma: no cover
        return
    if getattr(av.open, "_lessonforge_patched", False):
        return

    original = av.open

    def compat_open(file, mode="r", *args, **kwargs):
        kwargs.pop("metadata_errors", None)
        return original(file, mode, *args, **kwargs)

    compat_open._lessonforge_patched = True  # type: ignore[attr-defined]
    av.open = compat_open  # type: ignore[assignment]


def _ensure_cuda_dlls() -> None:
    """让 CTranslate2 找得到 CUDA 12 运行时库。

    很多 Windows 机器只有显卡驱动、没装 CUDA Toolkit，ctranslate2 就会报
    "Library cublas64_12.dll is not found"。这里把三类可能的 DLL 目录注册进
    DLL 搜索路径：

    1. pip 装的 nvidia-*-cu12 包（requirements-gpu.txt 装的就是它）
    2. ctranslate2 自带的 cudnn64_9.dll
    3. 装了完整 CUDA Toolkit 时的 v*/bin
    """
    if os.environ.get("LESSONFORGE_CUDA_DLLS_READY"):
        return
    import site

    roots: list[Path] = []
    for getter in (site.getsitepackages, site.getusersitepackages):
        try:
            roots.extend(Path(sp) for sp in getter())
        except Exception:  # noqa: BLE001
            pass

    dirs: list[str] = []

    # 1) nvidia-*-cu12 包：site-packages\nvidia\<pkg>\bin\cudlas64_12.dll
    for base in roots:
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for dll in nvidia.rglob("*.dll"):
            parent = str(dll.parent)
            if parent not in dirs:
                dirs.append(parent)

    # 2) ctranslate2 自带的 cudnn64_9.dll
    try:
        import ctranslate2

        own = str(Path(ctranslate2.__file__).parent)
        if own not in dirs:
            dirs.append(own)
    except Exception:  # noqa: BLE001
        pass

    # 3) 完整 CUDA Toolkit 的 bin
    try:
        for pattern in (
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\bin",
            r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v*\lib\x64",
        ):
            for d in glob.glob(pattern):
                if d not in dirs:
                    dirs.append(d)
    except Exception:  # noqa: BLE001
        pass

    for d in dirs:
        try:
            _DLL_HANDLES.append(os.add_dll_directory(d))  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

    # 按全路径预加载依赖，让后面的裸名字加载能命中已加载模块
    for lib in _PRELOAD_LIBS:
        for d in dirs:
            path = os.path.join(d, lib)
            if not os.path.exists(path):
                continue
            try:
                _DLL_HANDLES.append(ctypes.WinDLL(path))  # type: ignore[attr-defined]
            except OSError:
                pass
            break

    if dirs:
        os.environ["PATH"] = ";".join(dirs) + os.pathsep + os.environ.get("PATH", "")
    os.environ["LESSONFORGE_CUDA_DLLS_READY"] = "1"


def _cuda_runtime_ready() -> bool:
    """CUDA 运行时库到底能不能加载。

    这一步不能省：CTranslate2 是**懒加载** cuBLAS 的，
    `WhisperModel(device="cuda")` 构造时不会报错，真正开始推理才炸。
    所以选设备之前必须先用 ctypes 真的 load 一次。

    注意查的是 cublas64_12.dll，不是 cudlas64_12.dll——
    后者根本不存在于任何 CUDA 安装里。CTranslate2 的报错文案
    写的是 cudlas64_12.dll，那是它的措辞问题，别被带偏。
    """
    if os.name != "nt":
        return True
    for name in ("cublas64_12.dll", "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)  # type: ignore[attr-defined]
        except OSError:
            return False
    return True


def _cuda_missing_hint() -> str:
    return (
        "没找到 CUDA 12 运行时库（cudlas64_12.dll）。"
        "显卡驱动在，但 cuBLAS/cuDNN 不在。"
        "想用显卡提速就装：pip install -r requirements-gpu.txt"
    )


def _is_cuda_missing(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("cublas", "cudnn", "cudart", "cuda", "not found or cannot be loaded"))


def _pick_device() -> str:
    """选设备。注意：光有驱动不够，必须真能 load 到 cuBLAS 才用 GPU。"""
    if SETTINGS.whisper_device != "auto":
        return SETTINGS.whisper_device
    try:
        import ctranslate2

        _ensure_cuda_dlls()
        if ctranslate2.get_cuda_device_count() > 0 and _cuda_runtime_ready():
            return "cuda"
    except Exception:  # noqa: BLE001
        pass
    return "cpu"


def _pick_compute(device: str) -> str:
    if device != "cuda":
        return "int8"
    compute = SETTINGS.whisper_compute
    return compute or "float16"


def load_model(device: str | None = None) -> tuple[object, str, str]:
    """加载（并缓存）Whisper 模型。返回 (模型, 设备, 模型规格)。

    注意：这里返回成功**不代表** CUDA 真能用——cuBLAS 是懒加载的，
    真正的兜底在 transcribe() 里包住实际推理。
    """
    from faster_whisper import WhisperModel

    _patch_av_open()
    _ensure_cuda_dlls()
    device = device or _pick_device()

    attempts = [
        (SETTINGS.whisper_model, _pick_compute(device)),
        ("medium", "int8"),
        ("small", "int8"),
    ]

    last: Exception | None = None
    for size, compute in attempts:
        key = (size, device, compute)
        if key in _MODEL_CACHE:
            return _MODEL_CACHE[key], device, size
        try:
            model = WhisperModel(size, device=device, compute_type=compute)
            _MODEL_CACHE[key] = model
            if device == "cuda":
                os.environ.setdefault("LESSONFORGE_CUDA_OK", "1")
            return model, device, size
        except Exception as exc:  # noqa: BLE001
            last = exc
            # 构造阶段就报 CUDA 缺库，别在同一个错误上反复试，直接换 CPU
            if _is_cuda_missing(exc):
                break

    if device == "cuda":
        return load_model("cpu")
    raise RuntimeError(f"Whisper 模型加载失败：{last}")


def _transcribe_once(
    model: object,
    audio: Path,
    duration: float,
    progress: Progress | None,
    hint: str,
    device: str,
    size: str,
) -> list[Cue]:
    """跑一次转写。真正的 GPU 推理就发生在下面那个 for 里。"""
    compute = _pick_compute(device)
    if progress:
        progress(0.0, f"加载 Whisper 模型（{size} / {device} / {compute}）")

    language = SETTINGS.whisper_language
    if language in {"auto", ""}:
        language = None

    beam_sz = int(os.getenv("WHISPER_BEAM_SIZE", "2"))
    segments, info = model.transcribe(
        str(audio),
        language=language,
        task="transcribe",
        beam_size=beam_sz,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 400},
        word_timestamps=True,
        condition_on_previous_text=False,
        initial_prompt=hint or "这是一段中文课程教学视频，包含数学、物理、化学等学科的讲解、公式推导和例题讲解。",
    )

    total = getattr(info, "duration", None) or duration or 0.0
    cues: list[Cue] = []
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        clean = to_simplified(text)
        cues.append(Cue(start=float(seg.start), end=float(seg.end), text=clean))
        if progress and total:
            progress(
                min(0.99, float(seg.end) / total),
                f"转写中 {_hms(seg.end)} / {_hms(total)}  「{clean[:26]}…」",
            )
    if progress:
        progress(1.0, f"转写完成，共 {len(cues)} 段字幕")
    return cues


def transcribe(
    audio: Path,
    duration: float,
    progress: Progress | None = None,
    hint: str = "",
) -> list[Cue]:
    """把音频转成带时间戳的字幕段。

    CTranslate2 懒加载 CUDA 库：模型构造成功不代表推理能用，
    第一次真正跑 GPU 才会抛 "Library cublas64_12.dll is not found"。
    所以兜底必须包在**实际推理**外面，而不是只包模型构造。
    """
    model, device, size = load_model()
    try:
        return _transcribe_once(model, audio, duration, progress, hint, device, size)
    except Exception as exc:  # noqa: BLE001
        if device != "cuda" or not _is_cuda_missing(exc):
            raise
        if progress:
            progress(0.0, f"{_cuda_missing_hint()} —— 自动改用 CPU 重跑，会慢一些")
        model, device, size = load_model("cpu")
        return _transcribe_once(model, audio, duration, progress, hint, device, size)


def to_simplified(text: str) -> str:
    """统一转成简体。

    Whisper 在中文长音频上有时会中途漂到繁体（"剛好在一個"），
    混在字幕里会让后面的知识点切分和教案文本都不对味。
    """
    if not text:
        return text
    try:
        import zhconv

        return zhconv.convert(text, "zh-cn")
    except ImportError:
        return text


def to_text(cues: list[Cue], with_time: bool = True) -> str:
    lines = []
    for c in cues:
        if with_time:
            lines.append(f"[{_hms(c.start)}] {c.text}")
        else:
            lines.append(c.text)
    return "\n".join(lines)


def save(cues: list[Cue], path: Path) -> None:
    path.write_text(
        json.dumps([c.model_dump() for c in cues], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load(path: Path) -> list[Cue]:
    if not path.exists():
        return []
    return [Cue(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


def _hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"