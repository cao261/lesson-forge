"""命令行入口：不依赖浏览器也能跑。

    python run.py                          # 启动网页界面并自动开浏览器
    python run.py <视频链接>                # 直接生成一份教案
    python run.py --batch 链接列表.txt      # 批量：一行一个链接，按顺序排队
"""
from __future__ import annotations

import socket
import sys
import threading
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import SETTINGS  # noqa: E402


_last = {"pct": -1.0, "msg": ""}


def _parse_flags(args: list[str]) -> dict:
    out: dict = {}
    for arg in args:
        if arg in {"--no-review", "--无审核"}:
            out["do_review"] = False
        elif arg in {"--fresh", "--重新下载"}:
            out["reuse"] = False
        elif arg.startswith("--subject="):
            out["subject"] = arg.split("=", 1)[1]
        elif arg.startswith("--grade="):
            out["grade"] = arg.split("=", 1)[1]
        elif arg.startswith("--minutes="):
            out["duration_min"] = float(arg.split("=", 1)[1])
    return out


def _port_alive(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _open_browser(url: str) -> None:
    """等服务真的起来再开浏览器，否则开过去是个"无法访问此页面"。

    LESSONFORGE_NO_BROWSER=1 时不开——后台起服务（比如自动化跑批）不该
    在人桌面上弹浏览器出来。
    """
    import os
    import time

    if os.getenv("LESSONFORGE_NO_BROWSER", "").strip() not in ("", "0", "false"):
        return
    for _ in range(60):
        if _port_alive(SETTINGS.host, SETTINGS.port):
            break
        time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _progress(frac: float, message: str) -> None:
    """只在这一格进度有新动静时重绘，避免日志被刷爆。"""
    pct = int(frac * 200) / 2  # 0.5% 一档
    if pct == _last["pct"]:
        return
    _last["pct"] = pct
    _last["msg"] = message
    bar_len = 28
    filled = int(frac * bar_len)
    bar = "█" * filled + "·" * (bar_len - filled)
    sys.stdout.write(f"\r  [{bar}] {frac * 100:5.1f}%  {message[:70]:<70}")
    sys.stdout.flush()


def main() -> int:
    if len(sys.argv) < 2:
        from app.web.main import app  # noqa: F401
        import uvicorn

        url = f"http://{SETTINGS.host}:{SETTINGS.port}"

        # 已经有人在跑了就别再起一个，直接开浏览器，省得端口冲突报一堆天书
        if _port_alive(SETTINGS.host, SETTINGS.port):
            print(f"LessonForge 已经在运行：{url}")
            _open_browser(url)
            return 0

        print("=" * 62)
        print("  LessonForge · 视频转教案")
        print(f"  网页界面： {url}")
        print(f"  模型    ： {SETTINGS.model}   Key：{'已配置' if SETTINGS.has_llm else '未配置（请填 .env）'}")
        print(f"  Whisper ： {SETTINGS.whisper_model}   公式：{'Word 原生公式' if SETTINGS.mml2omml_xsl else '位图兜底'}")
        print("  停止服务：关掉这个黑窗口，或按 Ctrl+C")
        print("=" * 62)

        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
        uvicorn.run(app, host=SETTINGS.host, port=SETTINGS.port, log_level="warning")
        return 0

    from app.pipeline import JobOptions, run_pipeline

    # 批量：run.py --batch 链接列表.txt
    if sys.argv[1] == "--batch":
        if len(sys.argv) < 3:
            print("用法：python run.py --batch <每行一个链接的 txt>")
            return 2
        batch_file = Path(sys.argv[2])
        urls = [ln.strip() for ln in batch_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        extra = _parse_flags(sys.argv[3:])
        if not urls:
            print(f"{batch_file} 里没有链接")
            return 2
        print(f"批量处理 {len(urls)} 条，按顺序排队")
        ok, failed = 0, []
        for i, url in enumerate(urls, 1):
            print()
            print(f"──── [{i}/{len(urls)}] {url} ────")
            options = JobOptions(url=url, **extra)
            try:
                result = run_pipeline(options, _progress, lambda line: print(f"\n  · {line}"))
            except Exception as exc:  # noqa: BLE001
                print(f"\n  ✖ 失败：{type(exc).__name__}: {exc}")
                failed.append(url)
                continue
            ok += 1
            print(f"\n  ✅ {result.docx_path.name}")
        print()
        print(f"批量结束：成功 {ok} 条，失败 {len(failed)} 条")
        for u in failed:
            print(f"  ✖ {u}")
        return 0 if ok else 1

    url = sys.argv[1]
    options = JobOptions(url=url, **_parse_flags(sys.argv[2:]))
    print(f"开始处理：{url}")
    try:
        result = run_pipeline(options, _progress, lambda line: print(f"\n  · {line}"))
    except Exception as exc:  # noqa: BLE001
        import traceback

        print(f"\n失败：{type(exc).__name__}: {exc}")
        print("—— 出错位置 ——")
        traceback.print_exc()
        return 1
    print()
    print(f"✅ 教案已生成（两份）：")
    print(f"   教师备课版：{result.docx_path.name}")
    print(f"   学生讲义  ：{result.student_docx_path.name if result.student_docx_path else '—'}")
    print(f"   预览      ：{result.html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())