"""自检：think 剥离与 JSON 提取。

覆盖三个真实踩过的坑：
1. 流式被掐断时 <think> 没闭合，正文一个字都没有
2. 思考正文里全是 LaTeX 花括号，括号配平被带偏
3. 正文外还有别的对象时，别抓错片段
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.llm.client import LLMError, _extract_json, _scan_blobs, _strip_wrapper

CASES: list[tuple[str, bool, str]] = []


def expect(name: str, cond: bool, detail: str = ""):
    CASES.append((name, cond, detail))


# ---- 1. 正常闭合 think ----
closed = (
    r'<think>先想：公式有 \frac{\log_c b}{\log_c a} 这种，注意转义。'
    r'然后想：{x|x>0} 这种集合写法。</think>'
    '\n{"meta":{"title":"T"},"objectives":["a","b"],"knowledge":[{"id":"kp1"}]}'
)
got = _extract_json(closed)
expect("闭合 think 能取到完整 JSON",
       isinstance(got, dict) and got.get("objectives") == ["a", "b"], str(got)[:120])
expect("闭合 think 后无残留 <think>", "<think>" not in _strip_wrapper(closed))

# ---- 2. 未闭合 think（流式掐断）----
# 正文被吃掉了，剥完应该是空
unclosed = r'<think>我在算 \frac{a}{b} 和 {x} 这些式子，忽然断了'
expect("未闭合 think 会被整段切掉", _strip_wrapper(unclosed).strip() == "",
       repr(_strip_wrapper(unclosed)[:80]))

# 未闭合 think + 后面还跟了半个 JSON：
# 这属于"发出去的东西是残的"，应当明确报错，绝不能凑出一个半截教案
half = '<think>算 \\frac{a}{b} 中\n{"meta":{"title":"T"},"objectives":["a"]'
try:
    r = _extract_json(half)
    keys = sorted(r.keys()) if isinstance(r, dict) else []
    expect("未闭合 think 后的半截 JSON 被拒", False, f"竟然返回了 {keys}")
except LLMError:
    expect("未闭合 think 后的半截 JSON 被拒", True)

# ---- 3. 思考里 LaTeX 括号不配平，正文在后面 ----
tricky = (
    r'<think>推导：设 \frac{1}{2} < a，注意 a 的范围是 {x | x>0}，'
    r'再看 {y} 和 {z}，这些花括号都不配平，会计错。'
    r'另外还提到 \frac{log_c b}{log_c a} = 1。</think>'
    '\n{"meta":{"title":"正课"},"objectives":["x"],"flow":[{"stage":"导入"}],'
    '"knowledge":[{"id":"kp1","summary":"s"}]}'
)
r2 = _extract_json(tricky)
expect("思考里 LaTeX 括号不配平时仍取到正文",
       isinstance(r2, dict) and r2.get("objectives") == ["x"]
       and r2.get("flow") and r2.get("knowledge"),
       str(r2)[:160])

# ---- 4. 未闭合 think + LaTeX：不能返回"只有 meta 四字段"的空壳 ----
empty_shell = (
    r'<think>我在规划：标题要含 \frac{a}{b}，还要考虑 {x} 与 {y}。'
    r'此刻被网关掐断，后面的正文一个字节都没吐。'
)
try:
    r3 = _extract_json(empty_shell)
    got_keys = sorted(r3.keys()) if isinstance(r3, dict) else []
    bad = got_keys in (["duration_min", "grade", "subject", "title"], ["grade", "subject", "title"])
    expect("掐断的思考不会产出 meta 四字段空壳", not bad, f"拿到 {got_keys}")
except LLMError:
    expect("掐断的思考不会产出 meta 四字段空壳", True, "按预期抛 LLMError")

# ---- 5. _scan_blobs 确实跳过了 think 区间 ----
spans = list(_scan_blobs(tricky, "{", "}"))
expect("_scan_blobs 跳过 think 区间（只扫到 1 个顶层对象）",
       len(spans) == 1 and "正课" in spans[0], f"扫到 {len(spans)} 个片段")

# ---- 6. 正常无 think 的原文仍然照常 ----
plain = '{"a":1,"b":[1,2,3]}'
expect("无 think 的普通 JSON 不受影响", _extract_json(plain) == {"a": 1, "b": [1, 2, 3]})

# ---- 7. 多个 think 块 ----
multi = r'<think>a \frac{1}{2}</think>正文1<think>b {x}</think>{"k":"v"}'
expect("多个 think 块全部剥离", _extract_json(multi) == {"k": "v"}, str(_extract_json(multi)))

# ---- 8. 正文外有别的对象，取最长的主结构 ----
noisy = '备注：{"note":"短"}\n{"meta":{"title":"主"},"objectives":["a"],"knowledge":[1,2]}'
r4 = _extract_json(noisy)
expect("有噪声时取主结构而非第一个短片段",
       isinstance(r4, dict) and "objectives" in r4, str(r4)[:120])

# ---- 9. 代码围栏 ----
fenced = '```json\n{"a":"b"}\n```'
expect("代码围栏照常剥离", _extract_json(fenced) == {"a": "b"}, str(_extract_json(fenced)))


ok = 0
fail = 0
for name, passed, detail in CASES:
    if passed:
        ok += 1
        print(f"  ✅ {name}")
    else:
        fail += 1
        print(f"  ❌ {name}  →  {detail}")

print(f"\n自检 {ok}/{ok + fail} 通过")
sys.exit(0 if fail == 0 else 1)