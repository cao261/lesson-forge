"""候选帧打分：从 1fps 的连续帧里挑出"最可能是完整板书/完整题目"的几帧。

思路：整屏内容突变 = 老师切页/翻到新题；画面稳定 = 题目刚写完还没擦。
真正"题干完整"这件事交给多模态模型判断，这里只负责把最有可能的帧排到前面。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image


@dataclass
class FrameInfo:
    path: Path
    index: int
    ts: float
    diff_prev: float = 0.0
    ink_color: float = 0.0
    score: float = 0.0
    reason: str = ""


def _analyze_frame(path: Path) -> tuple[np.ndarray, float]:
    with Image.open(path) as img:
        # 画面指纹（灰度缩略）：用于判定场景切换与帧间差异
        fp = np.asarray(img.convert("L").resize((64, 36), Image.BILINEAR), dtype=np.float32) / 255.0
        # 彩色笔迹检测（RGB 缩略）：用于识别荧光笔划重点、红蓝标记笔迹
        thumb = img.convert("RGB").resize((160, 90), Image.BILINEAR)
        arr = np.asarray(thumb, dtype=np.int32)
        h, w = arr.shape[:2]
        # 聚焦题目主体区域，规避四角固定水印与底部播放条
        inner = arr[int(h * 0.1) : int(h * 0.85), int(w * 0.05) : int(w * 0.85)]
        r, g, b = inner[:, :, 0], inner[:, :, 1], inner[:, :, 2]
        max_c = np.maximum(np.maximum(r, g), b)
        min_c = np.minimum(np.minimum(r, g), b)
        # 高饱和度笔迹（荧光绿/亮黄、红笔圈画、蓝色标注）
        colored = (max_c - min_c > 45) & (max_c > 70)
        ink_color = float(np.mean(colored))
        return fp, ink_color


def analyze_frames(
    frames: list[Path],
    window_start: float,
    fps: float = 1.0,
    scene_threshold: float = 0.022,
) -> list[FrameInfo]:
    """算出每帧与其前一帧的差异量，并标记画面突变及笔迹量。"""
    infos: list[FrameInfo] = []
    prev_fp: np.ndarray | None = None
    for i, path in enumerate(frames):
        fp, ink_color = _analyze_frame(path)
        diff = 0.0 if prev_fp is None else float(np.mean(np.abs(fp - prev_fp)))
        infos.append(
            FrameInfo(
                path=path,
                index=i,
                ts=window_start + i / fps,
                diff_prev=diff,
                ink_color=ink_color,
            )
        )
        prev_fp = fp
    return infos


def is_scene_change(info: FrameInfo, threshold: float = 0.022) -> bool:
    return info.diff_prev >= threshold


def _ink_load(infos: list[FrameInfo], window: int = 8) -> list[float]:
    """每一帧的"板书密度"：最近若干帧的累计变化量。"""
    out: list[float] = []
    for i in range(len(infos)):
        start = max(0, i - window + 1)
        out.append(sum(x.diff_prev for x in infos[start : i + 1]))
    return out


def score_candidates(
    infos: list[FrameInfo],
    target_ts: float,
    scene_threshold: float = 0.022,
    top_k: int = 4,
    min_gap: float = 2.0,
) -> list[FrameInfo]:
    """按"离目标时刻的距离 + 切页刚稳定优先 + 零笔迹纯净度"综合打分。

    核心准则：学生版讲义配图必须是绝对空白的母版题面！
    老师在切页后的前 1~2 秒尚未动笔，此时题目最完整且最干净；
    一旦老师开讲，会立刻使用荧光笔划重点、红笔画圈、写坐标辅助线。
    因此切页后刚稳定的一瞬得分最高，任何带有彩色笔迹或滞后推导的帧予以强惩罚。
    """
    if not infos:
        return []

    changes = [i.index for i in infos if is_scene_change(i, scene_threshold)]

    scored: list[FrameInfo] = []
    for idx, info in enumerate(infos):
        # 1) 定位所属的切页区间
        prev_changes = [c for c in changes if c <= idx]
        if prev_changes:
            last_change = prev_changes[-1]
            frames_since = idx - last_change
            slide_segment = [infos[k] for k in range(last_change + 1, min(len(infos), idx + 1))]
        else:
            frames_since = -1
            slide_segment = infos[: idx + 1]

        # 2) 切页后新鲜度（刚铺开且稳定时最干净）：
        # frames_since == 0 为过渡中画面，打折；1~3 帧为母版黄金期；之后老师开始动笔，逐帧衰减
        if frames_since == 0:
            freshness = 0.15
        elif 1 <= frames_since <= 3:
            freshness = 1.0 - (frames_since - 1) * 0.06
        elif frames_since > 3:
            freshness = max(0.18, 0.85 - (frames_since - 3) * 0.05)
        else:
            freshness = 0.50

        # 3) 画面稳定度：当前帧相较前一帧变化越小，说明没有闪烁或翻页半截
        stability = 1.0 / (1.0 + info.diff_prev * 30.0)

        # 4) 时间贴合度：优先靠近 target_ts，但不偏向过晚（避免落入讲解后段）
        delay = info.ts - target_ts
        if delay < -3.0:
            time_score = 0.10
        elif -1.0 <= delay <= 3.0:
            time_score = 1.00
        else:
            time_score = math.exp(-((delay - 1.0) ** 2) / (2 * (10.0 ** 2)))

        # 5) 笔迹纯净度惩罚：检测相对本页母版的彩色荧光/马克笔涂鸦增量
        base_ink = min((x.ink_color for x in slide_segment), default=info.ink_color)
        ink_diff = max(0.0, info.ink_color - base_ink)
        pen_penalty = min(0.60, ink_diff * 45.0)

        # 滞后讲解惩罚
        tail_penalty = 0.30 if delay > 30.0 else 0.0

        info.score = (
            0.40 * freshness
            + 0.30 * stability
            + 0.30 * time_score
            - pen_penalty
            - tail_penalty
        )
        bits = [f"时间贴合={time_score:.2f}"]
        if 1 <= frames_since <= 3:
            bits.append("切页母版首帧")
        bits.append(f"稳定度={stability:.2f}")
        if pen_penalty > 0.05:
            bits.append(f"笔迹扣分={pen_penalty:.2f}")
        if tail_penalty:
            bits.append("偏后")
        info.reason = "，".join(bits)
        scored.append(info)

    scored.sort(key=lambda x: x.score, reverse=True)

    picked: list[FrameInfo] = []
    for info in scored:
        if len(picked) >= top_k:
            break
        if all(abs(info.ts - p.ts) >= min_gap for p in picked):
            picked.append(info)

    if not picked:
        picked = [min(scored, key=lambda x: abs(x.ts - target_ts))]
    return picked


def pick_board_frame(
    infos: list[FrameInfo],
    scene_threshold: float = 0.085,
    tail_fraction: float = 0.55,
) -> FrameInfo | None:
    """挑"知识点讲解结束、板书最完整"的那一帧：窗口后段里，画面稳定且接近末尾的一帧。"""
    if not infos:
        return None
    lo = int(len(infos) * tail_fraction)
    tail = infos[lo:]
    if not tail:
        tail = infos[-1:]

    # 末尾一段内，画面最稳定且时间靠后 → 板书写完了
    last_change = max(
        (i.index for i in tail if is_scene_change(i, scene_threshold)),
        default=tail[0].index - 1,
    )
    settled = [i for i in tail if i.index > last_change] or tail

    best = max(settled, key=lambda i: (i.index / max(1, len(infos)), -i.diff_prev))
    best.reason = "窗口后段画面稳定，板书最完整"
    return best


def _content_mask(img):
    """画面里"非背景"的像素掩码。课件背景近乎纯色，用四角中位色当基准。"""
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    h, w = gray.shape
    k = max(4, min(h, w) // 40)
    corners = np.concatenate([
        gray[:k, :k].ravel(), gray[:k, -k:].ravel(),
        gray[-k:, :k].ravel(), gray[-k:, -k:].ravel(),
    ])
    bg = float(np.median(corners))
    return (np.abs(gray - bg) > 22.0), bg


def _has_camera_corner(img) -> bool:
    """右下角是不是真的开着老师摄像头窗口。

    以前不管有没有摄像头都按"有"处理，把所有下半幅题目的右边界砍到 0.72。
    """
    arr = np.asarray(img, dtype=np.float32)
    h, w = arr.shape[:2]
    patch = arr[int(0.58 * h):, int(0.65 * w):]
    if patch.size == 0:
        return False
    # 摄像头窗口是块"与背景明显不同的杂乱方块"，方差大；纯色 PPT 区域方差很小。
    return float(patch.std()) > 48.0


def _grow_to_content(img, left: int, top: int, right: int, bottom: int):
    """把裁切框向外扩到内容边界：允许跳过少量空白间隙，但只扩不缩。

    模型给的 crop_box 边界经常估偏（实测把「则 sinα > 0」砍成「则 sin」）。
    与其信它，不如拿它当起点自己找内容：从框边朝外扫，连续空白超过 GAP 就停。
    最坏结果是留白变多，不会切字。单边扩张上限 MAXGROW，防止遇到水印把整幅都拉进来。
    """
    mask, _ = _content_mask(img)
    h, w = mask.shape
    cols = mask.sum(axis=0)
    rows = mask.sum(axis=1)

    gap = 14          # 允许跳过的空白像素：字间距、行间距都在这个量级以内
    max_w = max(8, int(w * 0.30))
    max_h = max(8, int(h * 0.30))

    def scan(vals: np.ndarray, start: int, step: int, limit: int, budget: int) -> int:
        i = start
        blank = 0
        while 0 <= i < limit and budget > 0:
            budget -= 1
            if vals[i] > 0:
                blank = 0
            else:
                blank += 1
                if blank > gap:
                    break
            i += step
        return i - step if i != start else start

    new_left = scan(cols, left, -1, 0, max_w)
    new_right = scan(cols, min(right, w - 1), +1, w, max_w)
    new_top = scan(rows, top, -1, 0, max_h)
    new_bottom = scan(rows, min(bottom, h - 1), +1, h, max_h)
    return (max(0, new_left), max(0, new_top), min(w, new_right + 1), min(h, new_bottom + 1))


def crop_problem_image(
    source_path: Path,
    dest_path: Path,
    crop_box: list[float] | tuple[float, ...] | None = None,
    padding: int = 18,
) -> Path:
    """智能裁剪例题画面：裁出题目与板书核心区域，剔除顶部系列横幅、四角水印及讲师摄像头，并增强画面锐度。"""
    try:
        from PIL import ImageEnhance, ImageFilter

        with Image.open(source_path) as orig:
            img = orig.convert("RGB")
            w, h = img.size

            # 1. 优先使用模型定位的题目边界框 (0.0~1.0)
            if crop_box and len(crop_box) == 4:
                b0, b1, b2, b3 = [float(x) for x in crop_box]
                if b0 > b2:
                    b0, b2 = b2, b0
                if b1 > b3:
                    b1, b3 = b3, b1
                if b2 > 1.0 or b3 > 1.0:
                    b0, b2 = b0 / 1000.0, b2 / 1000.0
                    b1, b3 = b1 / 1000.0, b3 / 1000.0

                # 自适应轴对换检测：
                # 课件通常为 16:9 横屏，题干沿水平方向排布（宽通常大于高，且占屏幕宽度的 40% 以上）。
                # 若模型将 [xmin, ymin, xmax, ymax] 误传为 [ymin, xmin, ymax, xmax]，
                # 会导致计算出的宽 (b3 - b1) 极窄（< 0.35）且高 (b2 - b0) 极高（> 0.40）。
                if (b3 - b1) < 0.35 and (b2 - b0) > 0.40:
                    ymin, ymax = b1, b3
                    xmin, xmax = b0, b2
                else:
                    ymin, ymax = b0, b2
                    xmin, xmax = b1, b3

                # 保护性边界扩展：
                # 1. 横向：题目左侧不能切掉题号（min 靠左），右侧往往有选项或图形
                xmin = max(0.0, min(xmin, 0.05))
                if ymax < 0.65:
                    # 上半部分无教师摄像头，右侧可完全包含题目和选项
                    xmax = min(0.98, max(xmax, 0.88))
                else:
                    # 下半部分要避开右下角讲师人像。**但只在那一角真的有人像时才收**——
                    # 以前这里不看画面就一刀切到 0.72，把上半幅里横排题干的**后半句**
                    # 直接砍没了（实测 ex8「则 sinα > 0」只剩「则 sin」）。
                    # 题干是横排的，它伸到哪儿跟它在不在下半幅毫无关系。
                    if _has_camera_corner(img):
                        xmax = min(0.72, max(xmax, 0.65))
                    else:
                        xmax = min(0.98, max(xmax, 0.88))

                # 2. 纵向：保留适度上下留白
                ymin = max(0.0, ymin - 0.02)
                ymax = min(0.98, ymax + 0.02)

                top = max(0, int(ymin * h) - padding)
                left = max(0, int(xmin * w) - padding)
                bottom = min(h, int(ymax * h) + padding)
                right = min(w, int(xmax * w) + padding)

                # 模型给的框只是"大致范围"，边界估得不准就会把题干切掉半截。
                # 这里从它出发向外找内容，碰到连续空白才停——只增不减，
                # 最坏情况是留白多一点，绝不会再切掉一个字。
                left, top, right, bottom = _grow_to_content(img, left, top, right, bottom)

                if (right - left) >= 64 and (bottom - top) >= 48:
                    cropped = img.crop((left, top, right, bottom))
                    cropped = _enhance_image(cropped)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    cropped.save(dest_path, "JPEG", quality=98)
                    return dest_path

            # 2. 本地启发式智能防人像与去无关杂质裁剪（兜底）
            arr = np.asarray(img, dtype=np.float32)
            gray = np.asarray(img.convert("L"), dtype=np.float32)

            corners = np.concatenate([
                gray[:15, :15].flatten(),
                gray[:15, -15:].flatten(),
                gray[-15:, :15].flatten()
            ])
            bg = float(np.median(corners))
            is_light = bg > 128

            diff = np.abs(gray - bg)
            mask = (diff > 20.0).astype(np.uint8)

            # 检测并剔除右下角讲师摄像头窗口 (x > 0.65*w, y > 0.58*h)
            c_y, c_x = int(0.58 * h), int(0.65 * w)
            br_patch = arr[c_y:, c_x:]
            if br_patch.size > 0:
                br_std = float(br_patch.std())
                if br_std > 48 or (is_light and float(arr[:c_y, :c_x, 2].mean() - br_patch[:, :, 2].mean()) > 18):
                    mask[c_y:, c_x:] = 0

            # 剔除顶部系列横幅（如视频开头固定的“三题搞定……”，与题目间有空白间隔）
            top_zone = int(0.18 * h)
            row_densities = mask[:top_zone, :].sum(axis=1)
            banner_rows = np.where(row_densities > 80)[0]
            cut_top = 0
            if len(banner_rows) > 0:
                b_end = banner_rows[-1]
                valleys = np.where(mask[b_end:top_zone, :].sum(axis=1) < 15)[0]
                if len(valleys) > 0:
                    cut_top = b_end + valleys[0]
                    mask[:cut_top, :] = 0

            # 剔除底部孤立水印（例如右下角/左下角的小文本标记）
            wm_h = int(0.86 * h)
            wm_w = int(0.68 * w)
            mask[wm_h:, wm_w:] = 0

            # 寻找主题目与板书内容边界（使用点密度过滤孤立噪点）
            col_counts = mask.sum(axis=0)
            row_counts = mask.sum(axis=1)
            valid_cols = np.where(col_counts > 4)[0]
            valid_rows = np.where(row_counts > 4)[0]

            if len(valid_cols) > 0 and len(valid_rows) > 0:
                xmin_idx = valid_cols[0]
                xmax_idx = valid_cols[-1]
                ymin_idx = valid_rows[0]
                ymax_idx = valid_rows[-1]

                top = max(cut_top, int(ymin_idx) - padding)
                bottom = min(h, int(ymax_idx) + padding)
                left = max(0, int(xmin_idx) - padding)
                right = min(w, int(xmax_idx) + padding)

                if (right - left) >= 64 and (bottom - top) >= 48:
                    cropped = img.crop((left, top, right, bottom))
                    cropped = _enhance_image(cropped)
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    cropped.save(dest_path, "JPEG", quality=98)
                    return dest_path

            # 若无需裁剪或识别失败，复制原图并适度增强清晰度
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            enhanced = _enhance_image(img)
            enhanced.save(dest_path, "JPEG", quality=98)
            return dest_path
    except Exception:
        # 异常兜底：若处理失败，回退到原始路径
        return source_path


def _enhance_image(img: Image.Image) -> Image.Image:
    """轻度锐化和对比度微调，让题目文字与几何图线条在讲义中更黑更清晰。"""
    try:
        from PIL import ImageEnhance, ImageFilter

        # 适度锐化：边缘更清晰，不产生过度噪点
        sharp = img.filter(ImageFilter.UnsharpMask(radius=1.2, percent=115, threshold=2))
        # 轻微增强对比度（1.05），让淡灰色笔迹更深
        enhancer = ImageEnhance.Contrast(sharp)
        return enhancer.enhance(1.05)
    except Exception:  # noqa: BLE001
        return img