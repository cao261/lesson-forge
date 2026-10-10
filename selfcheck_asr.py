"""Whisper 转写自检：截一段真实音频，跑通转写并输出带时间戳的字幕。"""
import subprocess
import sys
from pathlib import Path

WAV = Path("workspace/9bb432a3a1/audio.wav")
CLIP = Path("selfcheck_out/clip20s.wav")


def main() -> int:
    if not WAV.exists():
        print(f"找不到 {WAV}，请先跑一次下载")
        return 1
    CLIP.parent.mkdir(parents=True, exist_ok=True)
    if not CLIP.exists() or CLIP.stat().st_size < 1000:
        subprocess.run(
            ["ffmpeg", "-y", "-ss", "300", "-t", "20", "-i", str(WAV), str(CLIP)],
            capture_output=True, check=True,
        )
    print(f"测试音频：{CLIP} ({CLIP.stat().st_size / 1024:.0f} KB)")

    from app.stages import asr

    cues = asr.transcribe(CLIP, 20.0, progress=lambda f, m: print(f"  {f * 100:5.1f}%  {m}"))
    print(f"\n转写出 {len(cues)} 段：")
    for c in cues:
        print(f"  [{c.start:6.2f} → {c.end:6.2f}] {c.text}")
    assert cues, "转写结果为空"
    assert all(c.end >= c.start for c in cues), "存在时间戳倒挂"
    assert any(c.text.strip() for c in cues), "所有字幕都是空白"
    print("\n✅ Whisper 转写链路正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())