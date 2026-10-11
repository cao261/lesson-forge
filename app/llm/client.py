"""模型客户端：OpenAI 兼容 / Anthropic Messages 双协议 + 主备自动切换 + RPM 限流。

默认用 MiniMax-M3；它额度用完（insufficient balance / 余额不足）时，
自动切到备用模型继续跑，而不是让整条流水线失败。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from app.config import SETTINGS
from app.llm.routing import RateLimiter, classify_error

Notice = Callable[[str], None]

# 全局通知钩子：各阶段各自 new 客户端，靠它把"已降级""正在限速"这类事件
# 送到流水线的日志里，网页界面才能实时看到。
_NOTICE_HOOKS: list[Notice] = []


def set_notice_hook(fn: Notice | None) -> None:
    if fn is None:
        _NOTICE_HOOKS.clear()
    elif fn not in _NOTICE_HOOKS:
        _NOTICE_HOOKS.append(fn)


def emit_notice(message: str) -> None:
    for hook in _NOTICE_HOOKS:
        try:
            hook(message)
        except Exception:  # noqa: BLE001
            pass


def _coerce_object(value: Any) -> Any:
    """模型偶尔把整个对象包成数组返回（[{...}]）。解包出来。

    不这么做的话，调用方一个 data.get() 就会 AttributeError，
    整条流水线白跑十几分钟——这是实测踩到过的。
    """
    if not isinstance(value, list):
        return value
    dicts = [x for x in value if isinstance(x, dict)]
    if not dicts:
        return value
    if len(dicts) == 1:
        return dicts[0]
    # 只有当其中恰好有 1 个字典包含明显的大容器键（如 topic/segments/knowledge/meta/objectives），
    # 且其它字典只是附属 noise（例如 {"note": "..."}）时，才解包出该主体对象。
    main_keys = {"topic", "segments", "knowledge", "meta", "objectives", "modules"}
    main_candidates = [d for d in dicts if any(k in d for k in main_keys)]
    if len(main_candidates) == 1:
        return main_candidates[0]
    # 否则这是一个真正的列表（如知识点列表、例题列表），必须完整保留整个列表！
    return value


class LLMError(RuntimeError):
    pass


# 值得重试的 HTTP 状态码：限流、网关抖动、服务过载。
# 注意 529（MiniMax 的"集群负载较高"）必须在内，否则一次过载就整段失败。
RETRYABLE_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
OVERLOAD_CODES = {429, 520, 521, 522, 523, 524, 529}


_STRUCTURAL = ",:}]"


def _quote_closes(text: str, i: int) -> bool:
    """text[i] 是字符串里的一个双引号，判断它是"内容里的引号"还是"收尾的引号"。

    判据：合法 JSON 里，一个字符串值必然紧跟 , : } ] 或结束。所以往后跳过空白
    看到这些字符就是收尾。`""` 相邻时前一个是内容、后一个是收尾。
    """
    j = i + 1
    while j < len(text) and text[j] in " \t\r\n":
        j += 1
    if j >= len(text) or text[j] in _STRUCTURAL:
        return True
    if text[j] == '"':
        k = j + 1
        while k < len(text) and text[k] in " \t\r\n":
            k += 1
        if k >= len(text) or text[k] in _STRUCTURAL:
            return False  # 当前这个是内容里的引号，下一个才是收尾
    return False


def _escape_loose_quotes(text: str) -> str:
    """给 JSON 字符串里未转义的英文双引号补上反斜杠。

    M3 这类模型写中文时经常直接打 "例2 / 变式" 而不是 \\"例2 / 变式\\"，
    整段 JSON 从第一个内层引号起就废掉。对本来合法的 JSON 这是恒等操作。
    """
    out: list[str] = []
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if not in_str:
            if ch == '"':
                in_str = True
            out.append(ch)
            continue
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == "\\":
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            if _quote_closes(text, i):
                out.append(ch)
                in_str = False
            else:
                out.append('\\"')
            continue
        out.append(ch)
    return "".join(out)


def _strip_wrapper(text: str) -> str:
    """去掉 <think>…</think> 和 ```…``` 包裹，先后顺序不限，循环到稳定。

    另外要处理"<think> 没闭合"：流式下模型被中途掐断时经常这样。
    这时 <think> 之后全是思考内容、一个正文字符都没有，
    而思考里全是 LaTeX（\frac{a}{b} 这类），括号严重不平衡。
    不切掉的话后面 _scan_blobs 会从思考里的第一个 { 开始乱扫，
    抓出一个碰巧能解析的片段当答案——实测会返回只剩 meta 那 4 个字段的
    "空壳教案"，而且飘忽不定，正是"抽卡"的根源。
    """
    if "<think>" in text and "</think>" not in text:
        before = text.split("<think>", 1)[0].strip()
        if before:
            text = before
        else:
            after = text.split("<think>", 1)[1]
            for marker in ("```json", "```", "{\n", '{"', "[\n", "[{"):
                if marker in after:
                    text = marker + after.split(marker, 1)[1]
                    break
            else:
                # 一个标记都没找到：正文一个字节都没吐出来，<think> 之后
                # 整段都是思考。必须清空，不能原样留着——留着它，
                # _scan_blobs 会去扫思考里那些不配平的 LaTeX 花括号。
                text = ""
    prev = None
    while prev != text:
        prev = text
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        text = text.strip()
        text = re.sub(r"^```[a-zA-Z]*[ \t]*\r?\n?", "", text)
        text = re.sub(r"\r?\n?```[ \t]*$", "", text)
        text = text.strip()
    return text


def _scan_blobs(text: str, opener: str, closer: str):
    """逐个吐出括号配平后的片段（供"输出前后有废话"时兜底）。

    必须跳过 <think>…</think> 区间：思考正文里全是 LaTeX，
    \frac{a}{b}、{x|x>0} 会把括号配平算错，从那里开始扫很容易
    抓出一个"碰巧合法但内容是思考"的片段。实测这会返回只剩
    meta 四字段的空壳教案，而且飘忽不定。
    """
    # 先算出思考区间，扫描时整段忽略
    spans: list[tuple[int, int]] = []
    pos = 0
    while True:
        i = text.find("<think>", pos)
        if i < 0:
            break
        j = text.find("</think>", i)
        if j < 0:
            spans.append((i, len(text)))
            break
        spans.append((i, j + len("</think>")))
        pos = j + 1

    def in_think(idx: int) -> bool:
        return any(a <= idx < b for a, b in spans)

    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_think(i):
            continue
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
            if depth == 1:
                start = i
        elif ch == closer and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                yield text[start : i + 1]


def _extract_json(text: str) -> Any:
    """从模型输出里稳健地抠出 JSON。

    依次尝试：剥掉 think/围栏后的正文 → 原文 → 补转义内嵌引号 → 括号扫描。

    顺序很重要：必须先剥 think 再解析。思考正文里全是 LaTeX 花括号，
    直接扫原文会抓到思考里的片段（实测返回只剩 meta 四字段的空壳教案，
    而且每次落在哪飘忽不定——这就是"抽卡"的根源）。
    括号扫描取**最长**的合法片段而不是第一个：模型偶尔会在正文外再裹一层
    对象或注释，第一个片段未必是主结构。
    """
    text = text.strip()
    seen: set[str] = set()
    for base in (_strip_wrapper(text), text):
        if not base or base in seen:
            continue
        seen.add(base)
        for variant in (base, _escape_loose_quotes(base)):
            try:
                return json.loads(variant)
            except json.JSONDecodeError:
                pass
            for opener, closer in (("{", "}"), ("[", "]")):
                # 取最长的合法片段。注意不能"碰到非空对象就返回"：
                # 正文前面常有 {"note":"短"} 这类噪声，先返回就会把主结构丢掉，
                # 只剩一个 note 字段——正是空壳教案的另一种长相。
                best: tuple[str, Any] | None = None
                for blob in _scan_blobs(variant, opener, closer):
                    try:
                        got = json.loads(blob)
                    except json.JSONDecodeError:
                        continue
                    if best is None or len(blob) > len(best[0]):
                        best = (blob, got)
                if best is not None:
                    return best[1]
    raise LLMError(f"模型输出里找不到合法 JSON，原始内容前 500 字：\n{text[:500]}")


def _is_retryable(status: int, exc: Exception) -> bool:
    if status in RETRYABLE_CODES:
        return True
    if status == 0 and isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return True
    text = str(exc)
    return any(f"[{code}]" in text for code in RETRYABLE_CODES)


def _downscale_image(path: Path, max_edge: int = 1568) -> tuple[bytes, str]:
    """把本地图片压到合理尺寸，返回 (数据, mime)。"""
    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(str(path))[0] or "image/png"
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if max(w, h) > max_edge:
                scale = max_edge / max(w, h)
                img = img.convert("RGB").resize(
                    (max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS
                )
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=92)
                return buf.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001
        pass
    return data, mime


_LIMITERS: dict[str, RateLimiter] = {}
_LIMITER_LOCK = threading.Lock()


def _shared_limiter(name: str, rpm: int) -> RateLimiter:
    """按 provider 名取**全局共享**的限速器。

    限速是服务端对"这个模型"的限制，不是对某个客户端对象的限制。
    以前每个 client 各拿一个限速器：串行时看不出问题（调用间隔本来就远大于
    6 秒），但一批任务并发时 N 个 client 会各自放行 10 RPM，叠加起来直接
    超过服务端限流，然后集体被拒、集体降级。共享之后才是真的在限流。
    """
    with _LIMITER_LOCK:
        lim = _LIMITERS.get(name)
        if lim is None or lim.rpm != max(0, int(rpm)):
            lim = RateLimiter(rpm)
            _LIMITERS[name] = lim
        return lim


class ModelClient:
    """对外还是 chat / chat_json / vision_json 三个方法，内部自动选模型。"""

    def __init__(self, notice: Notice | None = None) -> None:
        self.providers = list(SETTINGS.active_providers)
        self.limiters = {p["name"]: _shared_limiter(p["name"], p["rpm"]) for p in self.providers}
        self.active_index = 0
        self.switch_log: list[str] = []
        self._pending_switch: tuple[str, str] | None = None
        self._notice = notice
        # 反复鉴权失败的档位直接拉黑，避免每次调用都白跑一趟
        self._disabled: dict[str, str] = {}
        if not self.providers:
            raise LLMError(
                "没有配置任何模型密钥。请复制 .env.example 为 .env 并填入 PRIMARY_API_KEY。"
            )

    # ------------------------------------------------------------------ 路由
    @property
    def active(self) -> dict:
        return self.providers[self.active_index]

    def _note(self, message: str) -> None:
        # 用 getattr 而不是直接取：自检脚本用 __new__ 绕过 __init__ 借方法，
        # 这时候 switch_log / _notice 都不存在，提示信息不该把自检带崩。
        if getattr(self, "switch_log", None) is None:
            self.switch_log = []
        self.switch_log.append(message)
        if getattr(self, "_notice", None):
            self._notice(message)
        emit_notice(message)

    def _switch_to(self, index: int, reason: str) -> bool:
        if index == self.active_index or index >= len(self.providers):
            return False
        old, new = self.active, self.providers[index]
        self.active_index = index
        self._note(f"⚠ {old['name']}（{old['model']}）不可用：{reason} → 自动降级到 {new['name']}（{new['model']}）")
        return True

    # ------------------------------------------------------------------ 协议
    def _build_payload(self, provider: dict, messages: list[dict[str, Any]], *,
                       temperature: float, max_tokens: int, json_mode: bool,
                       thinking: str | None, stream: bool = False) -> dict[str, Any]:
        model = provider["model"]
        temp = SETTINGS.temperature if temperature is None else temperature
        tokens = max_tokens or SETTINGS.max_tokens
        style = provider.get("style", "openai")
        mode = thinking or SETTINGS.thinking

        if style == "anthropic":
            system_parts = [m["content"] for m in messages if m.get("role") == "system"]
            turns = [
                {"role": m["role"], "content": _anthropic_content(m["content"])}
                for m in messages if m.get("role") != "system"
            ]
            payload: dict[str, Any] = {
                "model": model,
                "max_tokens": tokens,
                "messages": turns,
                "stream": stream,
            }
            if system_parts:
                payload["system"] = "\n\n".join(system_parts)
            if temp is not None:
                payload["temperature"] = temp
            if mode and mode != "disabled":
                payload["thinking"] = {"type": "adaptive"}
            return payload

        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temp is not None:
            payload["temperature"] = temp

        if style == "minimax":
            # MiniMax 系列：max_completion_tokens + thinking.type + response_format
            payload["max_completion_tokens"] = tokens
            if mode:
                payload["thinking"] = {"type": mode}
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
        else:
            # 标准 OpenAI / Agnes 等：max_tokens，思考开关走 chat_template_kwargs，
            # 且多数实现不支持 response_format —— 靠提示词里"只输出 JSON"来约束，
            # 解析端也有兜底（见 _extract_json）。
            payload["max_tokens"] = tokens
            if mode and mode != "disabled":
                payload["chat_template_kwargs"] = {"enable_thinking": True}
        return payload

    def _parse_response(self, provider: dict, data: dict) -> str:
        if provider["protocol"] == "anthropic":
            blocks = data.get("content") or []
            text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            return text
        return (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""

    @staticmethod
    def _parse_stream(provider: dict, lines: Iterable[str]) -> str:
        """把 SSE 帧拼成完整正文。

        只取最终答案、丢掉思考流：MiniMax 的思考内容走 reasoning_content，
        老一些的实现走 <think>…</think>，两处都不并进来（有 _strip_think 兜底）。
        真正要留的只有 choices[0].delta.content（OpenAI 形状）和
        content_block_delta（Anthropic 形状）。
        """
        chunks: list[str] = []
        anthropic = provider["protocol"] == "anthropic"
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(":") or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get("error"):
                raise LLMError(f"流式返回报错：{json.dumps(data['error'], ensure_ascii=False)[:400]}")
            if anthropic:
                if data.get("type") == "content_block_delta":
                    piece = (data.get("delta") or {}).get("text")
                    if piece:
                        chunks.append(piece)
                continue
            choices = data.get("choices")
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if isinstance(piece, str):
                chunks.append(piece)
            elif isinstance(piece, list):  # 少数实现把 content 拆成内容块数组
                for part in piece:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        chunks.append(part["text"])
        return "".join(chunks)

    def _headers(self, provider: dict) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if provider["protocol"] == "anthropic":
            headers["x-api-key"] = provider["api_key"]
            headers["anthropic-version"] = "2023-06-01"
        else:
            headers["Authorization"] = f"Bearer {provider['api_key']}"
        return headers

    def _url(self, provider: dict) -> str:
        base = provider["base_url"].rstrip("/")
        tail = "messages" if provider["protocol"] == "anthropic" else "chat/completions"
        if base.endswith("/chat/completions") or base.endswith("/messages"):
            return base
        return f"{base}/{tail}"

    # ------------------------------------------------------------------ 调用
    def _heartbeat(self, stop: threading.Event, what: str, interval: float) -> None:
        """阻塞等待模型时定期报一声。

        没有它的话，一次动辄几分钟的大模型调用期间界面是完全不动的，
        分不清"正在算"和"已经卡死"——用户只会以为坏了。
        """
        t0 = time.monotonic()
        while not stop.wait(interval):
            el = time.monotonic() - t0
            self._note(f"⏳ {what} 仍在进行，已等待 {int(el // 60)} 分 {int(el % 60)} 秒")

    def _call(self, provider: dict, messages: list[dict[str, Any]], *,
              temperature: float | None = None, max_tokens: int | None = None,
              json_mode: bool = False, thinking: str | None = None,
              label: str = "llm") -> str:
        url = self._url(provider)
        headers = self._headers(provider)
        use_stream = bool(getattr(SETTINGS, "llm_stream", True))
        payload = self._build_payload(
            provider, messages, temperature=temperature, max_tokens=max_tokens,
            json_mode=json_mode, thinking=thinking, stream=use_stream,
        )

        deadline = time.monotonic() + SETTINGS.call_deadline
        last_error: Exception | None = None
        last_status = 0
        total_tries = SETTINGS.max_retries + 1
        trail: list[str] = []
        beat = max(10.0, float(getattr(SETTINGS, "heartbeat_seconds", 45)))

        for attempt in range(total_tries):
            if attempt and time.monotonic() > deadline:
                raise LLMError(
                    f"{provider['name']} 调用超过总时限 {SETTINGS.call_deadline:.0f}s，放弃。\n"
                    f"   每次尝试：{'；'.join(trail)}\n"
                    f"   下次可以把 MINIMAX_CALL_DEADLINE 调大，或改用 .env 里的 WHISPER_MODEL 之类的小参数重跑。"
                )
            limiter = self.limiters.get(provider["name"])
            waited = limiter.acquire() if limiter else 0.0
            if waited > 1.0:
                self._note(
                    f"⏱ {provider['name']} 限速 {provider['rpm']} RPM，等待 {waited:.0f}s 后再发"
                )
            last_status = 0
            self._note(
                f"→ 调用 {provider['name']} · {label} · 第 {attempt + 1}/{total_tries} 次"
                f"（超时上限 {min(SETTINGS.timeout, max(30.0, deadline - time.monotonic())):.0f}s）"
            )
            stop = threading.Event()
            threading.Thread(
                target=self._heartbeat,
                args=(stop, f"{provider['name']} · {label}", beat),
                daemon=True,
            ).start()
            started = time.monotonic()
            try:
                budget = min(SETTINGS.timeout, max(30.0, deadline - time.monotonic()))
                if use_stream:
                    # 流式：读超时按"多久没收到帧"算，不是整条请求的总时长。
                    # 只要上游还在推帧就一路续命，思考几分钟也没关系。
                    timeout = httpx.Timeout(
                        budget,
                        read=min(SETTINGS.llm_read_timeout, budget),
                    )
                    with httpx.Client(timeout=timeout) as client:
                        with client.stream("POST", url, headers=headers, json=payload) as resp:
                            last_status = resp.status_code
                            if last_status >= 400:
                                raise LLMError(f"[{last_status}] {resp.read().decode('utf-8', 'replace')[:600]}")
                            content = self._parse_stream(provider, resp.iter_lines())
                    empty_hint = "（流式已结束但没有任何内容帧）"
                else:
                    with httpx.Client(timeout=httpx.Timeout(budget)) as client:
                        resp = client.post(url, headers=headers, json=payload)
                    last_status = resp.status_code
                    if last_status >= 400:
                        raise LLMError(f"[{last_status}] {resp.text[:600]}")
                    content = self._parse_response(provider, resp.json())
                    empty_hint = json.dumps(resp.json(), ensure_ascii=False)[:300]
                if not content.strip():
                    raise LLMError(f"模型返回空内容：{empty_hint}")
                spent = time.monotonic() - started
                self._note(f"✓ {provider['name']} · {label} 用时 {spent:.0f}s")
                return content
            except Exception as exc:  # noqa: BLE001
                spent = time.monotonic() - started
                last_error = exc
                kind = classify_error(last_status, str(exc))
                trail.append(f"第{attempt + 1}次 {spent:.0f}s 失败（{kind}）：{str(exc)[:80]}")
                self._note(f"✗ {provider['name']} · {label} 第 {attempt + 1} 次失败（{spent:.0f}s）：{str(exc)[:120]}")

                # 额度用完 / 鉴权失效：换模型，而不是继续在死路上重试
                if kind in {"quota", "auth"}:
                    self._pending_switch = (kind, str(exc)[:160])
                    break
                # 服务过载：先重试，重试用尽后再考虑降级
                if kind == "overload" and last_status in OVERLOAD_CODES:
                    if attempt >= SETTINGS.max_retries and SETTINGS.switch_on_overload:
                        self._pending_switch = ("overload", f"HTTP {last_status}")
                        break
                if attempt >= SETTINGS.max_retries or not _is_retryable(last_status, exc):
                    break
                if time.monotonic() > deadline:
                    break
                base = min(2**attempt, 30)
                if last_status in OVERLOAD_CODES:
                    base = min(10 * (attempt + 1), 45)
                self._note(f"↻ {int(base)}s 后重试（{kind}）")
                time.sleep(base + random.uniform(0, 3))
            finally:
                # 必须放 finally：成功路径在 try 里就 return 了，
                # 放 except/else 里的话心跳线程永远停不下来，
                # 已经打完的调用还会继续刷"仍在进行"。
                stop.set()

        raise LLMError(
            f"{provider['name']}/{provider['model']} 调用失败（{label}，试了 {len(trail)} 次）：\n"
            + "\n".join(f"   · {t}" for t in trail)
            + f"\n   最后一次错误：{last_error}"
        )

    def _chat_once(self, messages, **kw) -> str:
        """在候选模型里依次尝试，返回第一个成功的结果。

        降级策略：任何一档失败，只要还有下一档就用下一档继续跑。
        主模型最常见的是"额度用完"，不降级的话整条流水线就废了。
        """
        self._pending_switch = None
        tried: list[str] = []
        skipped: list[str] = []
        start = self.active_index
        for index in range(start, len(self.providers)):
            provider = self.providers[index]
            if provider["name"] in self._disabled:
                skipped.append(provider["name"])
                continue
            if index != self.active_index:
                self.active_index = index
            tried.append(provider["name"])
            try:
                return self._call(provider, messages, **kw)
            except LLMError as exc:
                pending = self._pending_switch
                self._pending_switch = None
                has_next = index + 1 < len(self.providers)
                if pending and pending[0] == "auth":
                    # Key 无效/被删：本次会话内不再尝试这一档
                    self._disabled[provider["name"]] = pending[1]
                    self._note(f"⛔ {provider['name']} 鉴权失败，本次运行不再使用：{pending[1][:90]}")
                if has_next:
                    reason = pending[1] if pending else str(exc)
                    self._note(f"⚠ {provider['name']} 失败（{reason[:90]}），换下一个模型")
                    continue
                self._note(f"✖ 所有模型都失败了：{str(exc)[:120]}")
                raise
        parts = [f"已尝试：{'、'.join(tried)}"] if tried else []
        if skipped:
            parts.append(f"已禁用：{'、'.join(skipped)}")
        raise LLMError(f"没有可用的模型（{'；'.join(parts) or '全部档位均不可用'}）")

    # ------------------------------------------------------------------ 对外接口
    def chat(self, messages: list[dict[str, Any]], *, temperature: float | None = None,
             max_tokens: int | None = None, json_mode: bool = False,
             thinking: str | None = None, label: str = "llm") -> str:
        if SETTINGS.mock:
            from app.llm.mock import mock_chat

            return mock_chat(messages, label=label)
        return self._chat_once(
            messages, temperature=temperature, max_tokens=max_tokens,
            json_mode=json_mode, thinking=thinking, label=label,
        )

    def chat_json(self, messages: list[dict[str, Any]], *, temperature: float | None = None,
                  max_tokens: int | None = None, thinking: str | None = None,
                  label: str = "llm") -> Any:
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens,
                         json_mode=True, thinking=thinking, label=label)
        try:
            return _coerce_object(_extract_json(text))
        except LLMError as first:
            # 有些模型不支持 response_format，可能吐出带解释的半截 JSON。
            # 补一次"把上面那段修成合法 JSON"的追问，通常一次就好。
            repair = [
                *messages,
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": (
                    "上面那段输出不是合法 JSON（可能有解释文字、代码块标记，或被截断）。"
                    "请把它修成一份完整、合法的 JSON 并重新输出一遍，"
                    "只输出 JSON 本身，不要任何解释、不要代码块标记、不要省略任何字段。"
                    "注意：最外层必须是一个对象 { }，不要用数组 [ ] 包起来。"
                )},
            ]
            self._note("↻ 模型输出不是合法 JSON，已发起一次自动修复重试")
            fixed = self.chat(repair, temperature=0.0, max_tokens=max_tokens,
                              json_mode=True, thinking=thinking, label=label)
            try:
                return _coerce_object(_extract_json(fixed))
            except LLMError:
                raise first

    def chat_object(self, messages: list[dict[str, Any]], *, label: str = "llm",
                    max_tokens: int | None = None, thinking: str | None = None,
                    **kw: Any) -> dict[str, Any]:
        """同 chat_json，但保证拿到的是 dict。

        调用方（compose / review / revise / segment）全都按对象取字段，
        哪怕模型偶尔返回数组，也会直接 AttributeError 把整条流水线带崩。

        数组里能翻出字典就直接用。翻不出来（比如模型把知识点写成
        ["记忆与技巧：…", "过程与方法：…"] 这种纯字符串数组）时，
        **不要自己猜这些字符串该归到哪个字段**——那是知识点还是例题，
        猜错就是往教案里塞错内容。改成把原样内容退回给模型，
        让它自己按 schema 补齐结构，它比我们清楚要哪些字段。
        """
        data = self.chat_json(messages, label=label, max_tokens=max_tokens,
                              thinking=thinking, **kw)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            dicts = [x for x in data if isinstance(x, dict)]
            if dicts:
                if any("title" in d for d in dicts):
                    return {"segments": dicts}
                return max(dicts, key=len)

        # ---- 结构对不上：把内容原样还给它，让它自己套 schema ----
        repair = [
            *messages,
            {"role": "assistant", "content": json.dumps(data, ensure_ascii=False)[:4000]},
            {"role": "user", "content": (
                "上面那份内容本身没问题，但它的**结构**和原始要求不一致："
                "最外层是一个数组，不是对象。\n"
                "请把它重新组织成原始要求里那个 JSON 对象：字段名、层级、"
                "每项下面该有哪些子字段，都必须和原始要求完全一致。\n"
                "内容不许丢、不许自己编——原样搬进对应字段即可。\n"
                "只输出 JSON，不要解释，不要代码块标记。"
            )},
        ]
        self._note("↻ 模型返回了数组而不是对象，已让它按结构重排一次")
        try:
            fixed = self.chat(repair, temperature=0.0, max_tokens=max_tokens,
                              json_mode=True, thinking=thinking, label=label)
            data = _coerce_object(_extract_json(fixed))
        except LLMError:
            data = None
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            dicts = [x for x in data if isinstance(x, dict)]
            if dicts:
                if any("title" in d for d in dicts):
                    return {"segments": dicts}
                return max(dicts, key=len)

        raise LLMError(
            f"模型没有返回 JSON 对象，而是 {type(data).__name__}：{str(data)[:200]}"
        )

    def vision_json(self, prompt: str, images: Iterable[Path], *, temperature: float = 0.1,
                    max_tokens: int = 8000, label: str = "vision") -> Any:
        if SETTINGS.mock:
            from app.llm.mock import mock_chat

            return _extract_json(mock_chat([{"role": "user", "content": prompt}], label=label))

        # 逐张试：有的免费模型不支持图片输入，逐张降级到能看图的那一档
        last_error: Exception | None = None
        for index in range(self.active_index, len(self.providers)):
            provider = self.providers[index]
            if index != self.active_index:
                self.active_index = index
            if not provider["vision"]:
                last_error = LLMError(f"{provider['name']} 配置为不支持图片输入")
                self._note(f"⏭ {provider['name']} 不支持图片输入，换用下一个模型")
                continue
            try:
                parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
                for img in images:
                    data, mime = _downscale_image(Path(img))
                    b64 = base64.b64encode(data).decode()
                    if provider["protocol"] == "anthropic":
                        parts.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": mime, "data": b64},
                        })
                    else:
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{b64}", "detail": "default"},
                        })
                text = self._chat_once(
                    [{"role": "user", "content": parts}],
                    temperature=temperature, max_tokens=max_tokens,
                )
                return _extract_json(text)
            except LLMError as exc:
                last_error = exc
                self._note(f"⚠ {provider['name']} 看图失败，尝试下一个模型：{str(exc)[:120]}")
        raise LLMError(f"所有模型都无法处理图片：{last_error}")


def _anthropic_content(content: Any) -> Any:
    """把 OpenAI 风格的 content 转成 Anthropic 的 content 块。"""
    if isinstance(content, str):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content or []:
        if part.get("type") == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url", "")
            if url.startswith("data:"):
                header, b64 = url.split(",", 1)
                media = header[5:].split(";")[0] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": b64},
                })
    return blocks or ""


# 旧名字保留，外部调用不用改
MiniMaxClient = ModelClient