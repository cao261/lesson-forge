# -*- coding: utf-8 -*-
"""自检：例题图裁切不许切字。

背景：模型给的 crop_box 只是"大致范围"，边界估偏就会把题干切掉半截。
实测 ex8「则 sinα > 0」被砍成「则 sin」。两道防线：
  1) 右下角真有摄像头窗口才收右边界（以前不看画面就一刀切到 0.72）；
  2) _grow_to_content 从模型框向外扩到内容边界，只扩不缩。
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app.media.frames import _content_mask, _grow_to_content, _has_camera_corner, crop_problem_image

TMP = Path(__file__).resolve().parent / "selfcheck_out" / "_crop"
TMP.mkdir(parents=True, exist_ok=True)
fails: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'OK  ' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def make_slide(w=1280, h=720, camera=False, text_right=0.78) -> Image.Image:
    """一张课件式画面：题目横排文字，右端到 text_right。"""
    img = Image.new("RGB", (w, h), (252, 252, 250))
    d = ImageDraw.Draw(img)
    d.text((int(0.18 * w), int(0.28 * h)), "直线的倾斜角为 a，则 sin a > 0 .",
           fill=(20, 20, 20))
    # 模拟文字的实际墨迹范围
    d.rectangle([int(0.18 * w), int(0.30 * h), int(text_right * w), int(0.36 * h)],
                fill=(20, 20, 20))
    if camera:
        d.rectangle([int(0.70 * w), int(0.62 * h), int(0.98 * w), int(0.97 * h)],
                    fill=(90, 130, 180))
    return img


def ink_bbox(img: Image.Image):
    mask, _ = _content_mask(img)
    cols = np.where(mask.sum(axis=0) > 0)[0]
    rows = np.where(mask.sum(axis=1) > 0)[0]
    return (cols[0], rows[0], cols[-1], rows[-1])


# ---------------------------------------------------------------- 1. 摄像头检测
slide = make_slide(camera=False)
check("纯课件画面不误报摄像头", _has_camera_corner(slide) is False)
slide_cam = make_slide(camera=True)
check("真有摄像头窗口能检出", _has_camera_corner(slide_cam) is True)

# ---------------------------------------------------------------- 2. 外扩只增不减
img = make_slide(text_right=0.78)
w, h = img.size
ink_l, ink_t, ink_r, ink_b = ink_bbox(img)

for name, box in [("框正好卡在文字右端", [ink_l / w, ink_t / h, 0.55, ink_b / h]),
                  ("框缩到一半", [ink_l / w, ink_t / h, (ink_l + ink_r) / 2 / w, ink_b / h])]:
    left = max(0, int(box[1] * w) - 18)
    top = max(0, int(box[0] * h) - 18)
    right = min(w, int(box[2] * w) + 18)
    bottom = min(h, int(box[3] * h) + 18)
    nl, nt, nr, nb = _grow_to_content(img, left, top, right, bottom)
    check(f"{name}：外扩后不缩", (nl, nt, nr, nb) != (left, top, right, bottom)
          and nl <= left and nt <= top and nr >= right and nb >= bottom,
          f"({left},{top},{right},{bottom}) -> ({nl},{nt},{nr},{nb})")
    check(f"{name}：右边界至少到墨迹末端", nr >= ink_r, f"墨迹到 {ink_r}，新右边界 {nr}")

# ---------------------------------------------------------------- 3. 端到端不切字
for label, cam, tbox in [("无摄像头 + 整片框", False, [0.05, 0.08, 0.95, 0.60]),
                         ("有摄像头 + 整片框", True, [0.05, 0.08, 0.95, 0.95])]:
    src = TMP / f"src_{int(cam)}.jpg"
    dst = TMP / f"out_{int(cam)}.jpg"
    make_slide(camera=cam).save(src, "JPEG", quality=95)
    crop_problem_image(src, dst, crop_box=tbox)
    out = Image.open(dst)
    ol, ot, orr, ob = ink_bbox(out)
    # 裁完的图里，右边界不该还有墨迹（=切到字了）
    check(f"{label}：右边界无残留墨迹", orr < out.width - 3, f"右 {orr} / 宽 {out.width}")
    check(f"{label}：下边界无残留墨迹", ob < out.height - 3, f"下 {ob} / 高 {out.height}")
    check(f"{label}：尺寸合法", out.width >= 64 and out.height >= 48, f"{out.size}")

# ---------------------------------------------------------------- 4. 取不到内容也不炸
blank = Image.new("RGB", (1280, 720), (255, 255, 255))
blank_src = TMP / "blank.jpg"
blank.save(blank_src, "JPEG")
res = crop_problem_image(blank_src, TMP / "blank_out.jpg", crop_box=[0.1, 0.1, 0.9, 0.9])
check("纯空白画面不崩溃", res.exists() and Image.open(res).size[0] > 0,
      str(Image.open(res).size if res.exists() else "无输出"))

print()
if fails:
    print(f"❌ {len(fails)} 项不通过：" + "、".join(fails))
    raise SystemExit(1)
print("✅ 裁切只外扩不切字，摄像头检测不再一刀切")