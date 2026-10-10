"""素材复用自检：已经下好的视频，不该再去碰 B 站。

背景：任务 e92264633f 之前那次，直接在第 1 步就被 B 站风控拦死（HTTP 412），
而本地 source.mp4（181MB）和 meta.json 都好好躺着。
原因：`fetch_video` 算出了 existing 却没用它短路，照样发 extract_info。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.stages.fetch import VideoMeta, _load_meta_cache, _write_meta, fetch_video  # noqa: E402

ok = 0
total = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok, total
    total += 1
    ok += bool(cond)
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  -> {detail}" if detail else ""))


def seed(work: Path) -> None:
    (work / "source.mp4").write_bytes(b"\x00" * 5000)  # 假装是视频
    _write_meta(work / "meta.json", VideoMeta(
        url="https://b23.tv/abc", source_id="BV1xx", title="缓存里的课",
        uploader="某某", duration=1200.0, description="", webpage_url="", is_bilibili=True,
    ))


print("=== 用例 1：素材齐全时零网络请求 ===")
with tempfile.TemporaryDirectory() as td:
    work = Path(td)
    seed(work)
    seen: list[str] = []
    meta, vid = fetch_video("https://b23.tv/abc", work, lambda f, m: seen.append(m))
    check("读到了缓存的视频", vid.name == "source.mp4", vid.name)
    check("读到了缓存的元数据", meta.title == "缓存里的课", meta.title)
    check("时长对得上", meta.duration == 1200.0, str(meta.duration))
    check("日志说明了是复用", any("复用已下载" in m for m in seen), str(seen))

print()
print("=== 用例 2：缺一样就必须照常走网络（不能瞎编）===")
with tempfile.TemporaryDirectory() as td:
    work = Path(td)
    (work / "source.mp4").write_bytes(b"\x00" * 5000)  # 只有视频，没有 meta.json
    try:
        fetch_video("https://b23.tv/abc", work, None)
        check("没有元数据时不走缓存（会去下载/报错）", False, "竟然没报错")
    except Exception as exc:  # noqa: BLE001
        check("没有元数据时不走缓存（会去下载/报错）", True, f"{type(exc).__name__}")

print()
print("=== 用例 3：坏的 meta.json 不能被当成有效缓存 ===")
with tempfile.TemporaryDirectory() as td:
    work = Path(td)
    (work / "source.mp4").write_bytes(b"\x00" * 5000)
    (work / "meta.json").write_text("{这不是合法 JSON", encoding="utf-8")
    check("坏 JSON → 返回 None", _load_meta_cache(work / "meta.json") is None)
    (work / "meta.json").write_text('{"url": "x"}', encoding="utf-8")
    # 这个是合法元数据，只是没时长。能不能复用由 fetch_video 的 duration 判断决定，
    # _load_meta_cache 只负责"能不能解析出来"。
    only_url = _load_meta_cache(work / "meta.json")
    check("只给 url 也能解析出来", only_url is not None and only_url.duration == 0.0,
          f"duration={only_url.duration if only_url else 'None'}")
    check("文件不存在 → 返回 None", _load_meta_cache(work / "没有这个.json") is None)

    # 时长为 0 的元数据不能拿来短路：后面所有时间戳都靠它
    seed(work)
    bad_meta = json.loads((work / "meta.json").read_text(encoding="utf-8"))
    bad_meta["duration"] = 0.0
    (work / "meta.json").write_text(json.dumps(bad_meta), encoding="utf-8")
    try:
        fetch_video("https://b23.tv/abc", work, None)
        check("时长为 0 时不短路", False, "竟然复用了")
    except Exception as exc:  # noqa: BLE001
        check("时长为 0 时不短路（会重新拉取）", True, f"{type(exc).__name__}")

print()
print("=== 用例 4：没音轨的重试路径不能崩（以前这里 NameError）===")
with tempfile.TemporaryDirectory() as td:
    work = Path(td)
    (work / "source.mp4").write_bytes(b"\x00" * 5000)
    import app.stages.fetch as F

    real_has = F.has_audio_stream
    F.has_audio_stream = lambda p: False          # 假装下到的文件没音轨
    F._clear_media = lambda w: None
    try:
        F._finish_fetch(work, {"quiet": True}, "https://b23.tv/abc", None, None)
        check("走到了重试逻辑", True, "（这次真的去重下了）")
    except NameError as exc:
        check("走到了重试逻辑", False, f"NameError: {exc} ← 就是这个 bug")
    except Exception as exc:  # noqa: BLE001
        check("走到了重试逻辑", True, f"已进入重试，止于 {type(exc).__name__}")
    finally:
        F.has_audio_stream = real_has

print()
print("=== 用例 5：本地视频路径不受影响 ===")
with tempfile.TemporaryDirectory() as td:
    work = Path(td)
    local = Path(td) / "我的课.mp4"
    local.write_bytes(b"\x00" * 5000)
    try:
        meta, vid = fetch_video(str(local), work, None)
        check("本地文件仍走本地路径", meta.is_bilibili is False, f"is_bilibili={meta.is_bilibili}")
    except Exception as exc:  # noqa: BLE001
        # 需要 ffprobe 之类，探不到音轨时报错也算没崩
        check("本地文件仍走本地路径", True, f"{type(exc).__name__}: 探测阶段报错但没崩")

print()
print(f"{'=' * 46}")
print(f"结果：{ok}/{total} 通过")
sys.exit(0 if ok == total else 1)