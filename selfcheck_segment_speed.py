"""segment 阶段实测：用真实缓存字幕跑一次，看关掉 thinking 后能不能在 600s 内跑完。

这是实测踩过的坑：整节课 1317 段 / 25740 字字幕喂进去，开着 adaptive thinking
会把 600 秒超时撑爆，重试 5 次全灭。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.models import Cue, LessonMeta  # noqa: E402
from app.stages import segment as seg  # noqa: E402

WORK = Path("workspace/9df0977112")

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


if not (WORK / "transcript.json").exists():
    print(f"找不到 {WORK / 'transcript.json'}，请先跑一次下载+转写")
    sys.exit(1)

cues = [Cue(**c) for c in json.loads((WORK / "transcript.json").read_text(encoding="utf-8"))]
meta_raw = json.loads((WORK / "meta.json").read_text(encoding="utf-8")) if (WORK / "meta.json").exists() else {}
meta = LessonMeta(
    source_title=meta_raw.get("title") or "未知",
    uploader=meta_raw.get("uploader") or "未知",
    title=meta_raw.get("title") or "",
    subject="", grade="",
    video_duration=float(meta_raw.get("duration") or 2820.0),
)
transcript_chars = sum(len(c.text) for c in cues)
print(f"字幕 {len(cues)} 段 / 约 {transcript_chars} 字 / 视频 {meta.video_duration/60:.1f} 分钟")
print(f"标题：{meta.source_title}\n")

print("=== 关掉 thinking 后实跑 segment ===")
t0 = time.time()
topic, subj, kps = seg.segment(
    meta, cues, meta.video_duration,
    progress=lambda f, m: print(f"  {f*100:5.1f}%  {m}"),
)
spent = time.time() - t0

n_ex = sum(len(k.examples) for k in kps)
print(f"\n用时 {spent:.0f}s（超时上限 600s）")
print(f"课题：{topic}")
print(f"知识点 {len(kps)} 个，例题 {n_ex} 道\n")
for k in kps:
    print(f"  {k.id:8s} [{k.ts_start/60:6.1f}–{k.ts_end/60:6.1f} 分] {k.title}  (例题 {len(k.examples)})")

check("在超时上限内跑完", spent < 540, f"{spent:.0f}s")
check("切出了知识点", len(kps) >= 3, f"{len(kps)} 个")
check("知识点时间轴在视频时长内",
      all(0 <= k.ts_start <= meta.video_duration and k.ts_end <= meta.video_duration + 1 for k in kps))
check("时间轴单调不倒挂", all(kps[i].ts_end <= kps[i + 1].ts_start + 0.5 for i in range(len(kps) - 1)))
check("识别出了例题", n_ex >= 1, f"{n_ex} 道")
check("例题时间点落在其知识点范围内",
      all(e.ts_start >= k.ts_start - 5 for k in kps for e in k.examples))
check("课题非空", bool(topic), topic)

print("\n" + "=" * 50)
print(f"通过 {ok}/{total}")
print("✅ segment 关掉 thinking 后正常" if ok == total else "❌ segment 仍有问题")
sys.exit(0 if ok == total else 1)
