"""连通性自检：文本调用 + 图片调用 + JSON 结构化输出，各打一发最小请求。

不打印 Key 本身，只报告是否可用。
"""
import base64
import io
import sys

import httpx

from app.config import SETTINGS

KEY = SETTINGS.api_key


def show(label: str, ok: bool, detail: str = "") -> None:
    mark = "OK  " if ok else "FAIL"
    print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))


def test_text() -> bool:
    try:
        with httpx.Client(timeout=90) as c:
            r = c.post(
                f"{SETTINGS.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                json={
                    "model": SETTINGS.model,
                    "messages": [{"role": "user", "content": "用一句话说明充分条件与必要条件的区别。"}],
                    "max_completion_tokens": 300,
                    "thinking": {"type": "disabled"},
                    "stream": False,
                },
            )
        if r.status_code >= 400:
            show("文本调用", False, f"HTTP {r.status_code}: {r.text[:300]}")
            return False
        text = r.json()["choices"][0]["message"].get("content", "")
        show("文本调用", bool(text.strip()), (text.strip()[:90] + "…") if text.strip() else "返回空")
        return bool(text.strip())
    except Exception as exc:  # noqa: BLE001
        show("文本调用", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return False


def test_json() -> bool:
    try:
        with httpx.Client(timeout=90) as c:
            r = c.post(
                f"{SETTINGS.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                json={
                    "model": SETTINGS.model,
                    "messages": [{"role": "user", "content": "只输出 JSON：{\"a\":1,\"b\":\"中文\"}"}],
                    "max_completion_tokens": 200,
                    "response_format": {"type": "json_object"},
                    "thinking": {"type": "disabled"},
                    "stream": False,
                },
            )
        if r.status_code >= 400:
            show("JSON 结构化输出", False, f"HTTP {r.status_code}: {r.text[:300]}")
            return False
        text = r.json()["choices"][0]["message"].get("content", "")
        from app.llm.client import _extract_json

        data = _extract_json(text)
        ok = isinstance(data, dict) and data.get("b") == "中文"
        show("JSON 结构化输出", ok, str(data)[:90])
        return ok
    except Exception as exc:  # noqa: BLE001
        show("JSON 结构化输出", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return False


def test_vision() -> bool:
    """用本地生成的一张"题面"图片测图片输入（看图转写题目全靠它）。"""
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        show("图片输入", False, "缺少 Pillow")
        return False
    img = Image.new("RGB", (900, 260), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.text((40, 60), "Solve: x^2 - 5x + 6 = 0", fill=(0, 0, 0))
    d.text((40, 140), "A. x=2 and x=3   B. x=-2 and x=-3", fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

    try:
        with httpx.Client(timeout=180) as c:
            r = c.post(
                f"{SETTINGS.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"},
                json={
                    "model": SETTINGS.model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "把图里的方程式用 LaTeX 写出来，只输出公式。"},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "default"}},
                    ]}],
                    "max_completion_tokens": 300,
                    "thinking": {"type": "disabled"},
                    "stream": False,
                },
            )
        if r.status_code >= 400:
            show("图片输入", False, f"HTTP {r.status_code}: {r.text[:300]}")
            return False
        text = r.json()["choices"][0]["message"].get("content", "")
        ok = "x" in text and ("5" in text or "6" in text)
        show("图片输入", ok, (text.strip()[:90] + "…") if text.strip() else "返回空")
        return ok
    except Exception as exc:  # noqa: BLE001
        show("图片输入", False, f"{type(exc).__name__}: {str(exc)[:200]}")
        return False


def main() -> int:
    print(f"端点：{SETTINGS.base_url}")
    print(f"模型：{SETTINGS.model}")
    print(f"Key ：{KEY[:8]}…{KEY[-4:]}（长度 {len(KEY)}）\n")
    if not KEY:
        print("没有配置 MINIMAX_API_KEY")
        return 1
    results = [test_text(), test_json(), test_vision()]
    print()
    if all(results):
        print("全部通过，可以开始跑真实教案生成。")
        return 0
    print("有项目未通过，请看上面的报错。")
    return 1


if __name__ == "__main__":
    sys.exit(main())