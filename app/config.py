"""全局配置：全部从 .env 读取，环境变量优先。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")

# HuggingFace 镜像：国内拉 Whisper 模型必需
os.environ.setdefault("HF_ENDPOINT", os.getenv("HF_ENDPOINT", "https://hf-mirror.com"))


def _env_bool(key: str, default: str = "0") -> bool:
    return os.getenv(key, default).strip().lower() in {"1", "true", "yes", "on"}


def _find_ffmpeg() -> str:
    explicit = os.getenv("FFMPEG_BIN", "").strip()
    if explicit:
        return explicit
    found = shutil.which("ffmpeg")
    if found:
        return found
    # winget 安装的常见位置
    base = Path(os.getenv("LOCALAPPDATA", "C:/Users")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        for candidate in base.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"):
            return str(candidate)
    return "ffmpeg"


def _find_mml2omml() -> str:
    explicit = os.getenv("MML2OMML_XSL", "").strip()
    if explicit and Path(explicit).exists():
        return explicit
    candidates = [
        r"C:\Program Files\Microsoft Office\root\Office16\MML2OMML.XSL",
        r"C:\Program Files (x86)\Microsoft Office\root\Office16\MML2OMML.XSL",
        r"C:\Program Files\Microsoft Office\Office16\MML2OMML.XSL",
        r"C:\Program Files (x86)\Microsoft Office\Office16\MML2OMML.XSL",
    ]
    for path in candidates:
        if Path(path).exists():
            return path
    bundled = ROOT / "assets" / "MML2OMML.XSL"
    return str(bundled) if bundled.exists() else ""


def _provider(
    prefix: str,
    default_name: str,
    default_base: str,
    default_model: str,
    protocol: str = "openai",
    rpm: int = 0,
    vision: bool = True,
    style: str = "",
) -> dict:
    def get(key: str, default: str = "") -> str:
        return os.getenv(f"{prefix}_{key}", default).strip()

    base = get("BASE_URL", default_base)
    resolved_protocol = get("PROTOCOL", protocol).lower()
    if not style:
        # 请求体风格：不同厂商的"思考开关""token 上限字段"叫法不一样
        if resolved_protocol == "anthropic":
            style = "anthropic"
        elif "minimax" in base.lower():
            style = "minimax"
        else:
            style = "openai"

    return {
        "name": get("NAME", default_name),
        "api_key": get("API_KEY", os.getenv("MINIMAX_API_KEY", "") if prefix == "PRIMARY" else ""),
        "base_url": base,
        "model": get("MODEL", default_model),
        "protocol": resolved_protocol,
        "style": style,
        "rpm": int(get("RPM", str(rpm)) or 0),
        "vision": _env_bool(f"{prefix}_VISION", "1" if vision else "0"),
    }


@dataclass(frozen=True)
class Settings:
    # ---- 模型 ----
    api_key: str = os.getenv("MINIMAX_API_KEY", "").strip()
    base_url: str = os.getenv("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1").rstrip("/")
    model: str = os.getenv("MINIMAX_MODEL", "MiniMax-M3")

    # 主备模型：额度用完自动降级（见 app/llm/routing.py）
    providers: tuple = (
        _provider("PRIMARY", "主模型", "https://api.minimaxi.com/v1", "MiniMax-M3"),
        _provider("FALLBACK", "备用模型", "https://apihub.agnes-ai.com/v1",
                  "agnes-3.0-flash", rpm=10),
    )
    fallback_enabled: bool = _env_bool("FALLBACK_ENABLED", "1")
    switch_on_overload: bool = _env_bool("FALLBACK_ON_OVERLOAD", "1")

    thinking: str = os.getenv("MINIMAX_THINKING", "adaptive")
    temperature: float = float(os.getenv("MINIMAX_TEMPERATURE", "0.2"))
    max_tokens: int = int(os.getenv("MINIMAX_MAX_TOKENS", "32000"))
    timeout: float = float(os.getenv("MINIMAX_TIMEOUT", "600"))
    max_retries: int = int(os.getenv("MINIMAX_MAX_RETRIES", "4"))
    call_deadline: float = float(
        os.getenv("MINIMAX_CALL_DEADLINE", "1500")
    )  # 单次调用（含所有重试）的总时限，防止服务端过载时把流水线卡死
    # 阻塞等待模型时，每隔多少秒往日志里报一次"还在进行，已等待 X"。
    # 不设心跳的话，一次几分钟的大调用期间界面完全不动，分不清在算还是卡死。
    heartbeat_seconds: float = float(os.getenv("LLM_HEARTBEAT_SECONDS", "45"))
    # 同一批里最多几个大模型调用同时在飞。
    # 注意这个数**不是**按 RPM 定的：主模型不限速，只有备用 agnes 限 10 RPM，
    # 而那个限速由 client._shared_limiter 全局共享、各并发线程一起匀速用。
    # 真正卡住的是"一批里本来就有几件独立的事"——compose 5 批、审核 4 批，
    # 所以取 6：能把一批全铺开，再高也没有更多活可干。
    # 也不宜更高：并发超了服务端会返 429/529，而这两个码被判成"过载"会触发降级，
    # 一轮过载可能把整批打到只有 10 RPM 的 agnes 上排队，反而比重试更慢。
    llm_parallel: int = int(os.getenv("LLM_PARALLEL", "6"))
    mock: bool = _env_bool("LESSONFORGE_MOCK")

    # ---- 语音转写 ----
    whisper_model: str = os.getenv("WHISPER_MODEL", "large-v3")
    whisper_device: str = os.getenv("WHISPER_DEVICE", "auto")
    whisper_compute: str = os.getenv("WHISPER_COMPUTE", "float16")
    whisper_language: str = os.getenv("WHISPER_LANGUAGE", "zh")

    # ---- 抓取 ----
    cookies_from_browser: str = os.getenv("BILI_COOKIES_FROM_BROWSER", "").strip()
    video_quality: str = os.getenv("VIDEO_QUALITY", "best")
    ffmpeg: str = _find_ffmpeg()

    # ---- 生成策略 ----
    shot_candidates: int = int(os.getenv("SHOT_CANDIDATES", "4"))
    shot_pre_roll: int = int(os.getenv("SHOT_PRE_ROLL", "3"))
    shot_post_roll: int = int(os.getenv("SHOT_POST_ROLL", "45"))
    max_review_rounds: int = int(os.getenv("MAX_REVIEW_ROUNDS", "2"))
    vision_review: bool = _env_bool("VISION_REVIEW")

    # ---- 其它 ----
    workspace: Path = Path(os.getenv("WORKSPACE_DIR", "workspace"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8760"))
    mml2omml_xsl: str = _find_mml2omml()

    @property
    def workspace_dir(self) -> Path:
        path = self.workspace if self.workspace.is_absolute() else ROOT / self.workspace
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def has_llm(self) -> bool:
        return any(p.get("api_key") for p in self.providers) and not self.mock

    @property
    def active_providers(self) -> tuple:
        """按优先级排好的可用模型列表（没有 Key 的自动剔除）。"""
        usable = [p for p in self.providers if p.get("api_key")]
        if not self.fallback_enabled:
            usable = usable[:1]
        return tuple(usable)


SETTINGS = Settings()