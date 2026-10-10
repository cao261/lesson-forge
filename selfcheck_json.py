"""JSON 解析容错自检：重点覆盖 M3 真实吐过的"中文里直接写英文双引号"这类坏输出。"""
import json

from app.llm.client import LLMError, _escape_loose_quotes, _extract_json

# ① 本次真实抓到的坏样本（截取自审核阶段实际返回）
REAL_BAD = '''<think>模型思考了一堆……

**事实核查：**

- 字幕有 ASR 错误
- 例2 / 变式 是编造的
</think>

```json
{
  "verdict": "revise",
  "score": 7.0,
  "hallucination_risk": "medium",
  "summary": "教案核心数学内容与字幕一致。但存在一处明显编造风险。",
  "issues": [
    {
      "severity": "major",
      "target": "knowledge[2].examples[0].title",
      "problem": "例题标题写为"例题：必要不充分条件求参数（例2 / 变式）·必要不充分条件求参数范围"，但截图文件名为 ex1_c1.jpg。",
      "suggestion": "去掉"（例2 / 变式）"标签，标题统一为"例题：必要不充分条件求参数范围"。"
    }
  ]
}
```'''

CASES = [
    ("① 真实坏样本（think + 代码围栏 + 中文内嵌引号）",
     REAL_BAD,
     lambda d: d["verdict"] == "revise" and "例2" in d["issues"][0]["problem"]),

    ("② 正常 JSON 不受影响",
     '{"a": 1, "b": ["x", "y"], "c": null}',
     lambda d: d["a"] == 1 and d["b"] == ["x", "y"] and d["c"] is None),

    ("③ 已有转义引号保持原样",
     '{"note": "他说\\"好\\"，然后走了"}',
     lambda d: d["note"] == '他说"好"，然后走了'),

    ("④ 代码围栏包裹的正常 JSON",
     '```json\n{"ok": true}\n```',
     lambda d: d["ok"] is True),

    ("⑤ 前后有解释文字",
     '好的，结果如下：\n{"v": "pass"}\n希望有帮助。',
     lambda d: d["v"] == "pass"),

    ("⑥ 末尾双引号相邻的病态写法",
     '{"p": "他说"你好""}',
     lambda d: d["p"] == '他说"你好"'),

    ("⑦ 数组根",
     '[{"id": "ex1"}, {"id": "ex2"}]',
     lambda d: len(d) == 2 and d[1]["id"] == "ex2"),

    ("⑧ 值里含反斜杠的 LaTeX",
     '{"latex": "$x^2-6<0$ 与 $a \\\\le 3$"}',
     lambda d: "a " in d["latex"] and "\\le" in d["latex"]),

    ("⑨ 引号后紧跟逗号不算内嵌",
     '{"a": "甲","b": "乙"}',
     lambda d: d["a"] == "甲" and d["b"] == "乙"),
]

print("=" * 74)
print("JSON 解析容错自检")
print("=" * 74)
passed = 0
for name, payload, check in CASES:
    try:
        data = _extract_json(payload)
        ok = check(data)
    except LLMError as exc:
        ok = False
        data = f"解析失败：{str(exc)[:120]}"
    print(f"  {'✅' if ok else '❌'} {name}")
    if not ok:
        print(f"       实际结果：{str(data)[:220]}")
    passed += ok

print("-" * 74)
print(f"  {passed}/{len(CASES)} 通过")

# 反例：纯自然语言必须报错，不能瞎抠出一个假 JSON
print()
print("=" * 74)
print("反向用例：不含 JSON 时必须明确报错")
print("=" * 74)
for bad in ["完全没有 JSON 的一段话", "模型拒绝回答。"]:
    try:
        _extract_json(bad)
        print(f"  ❌ 竟然解析成功了：{bad!r}")
    except LLMError:
        print(f"  ✅ 正确报错：{bad!r}")

# 恒等性：合法 JSON 经过补转义必须逐字不变
print()
print("=" * 74)
print("恒等性检查：合法 JSON 经 _escape_loose_quotes 不应改变")
print("=" * 74)
good = ['{"a": 1}', '{"s": "he said \\"hi\\""}', '[1,2,{"k":"v"}]', '{"e":"换行\\n制表\\t"}']
same = all(_escape_loose_quotes(g) == g for g in good)
print(f"  {'✅ 全部不变' if same else '❌ 有样本被改动了'}")

# 模型偶尔把对象包成数组返回，实测踩到过：调用方 data.get() 直接 AttributeError
print()
print("=" * 74)
print("顶层数组兜底：模型返回 [{...}] 时必须解包成对象")
print("=" * 74)
from app.llm.client import _coerce_object

WRAP_CASES = [
    ("① 单元素数组包对象（实测踩到的那种）",
     [{'meta': {'title': 'x'}, 'knowledge': []}],
     lambda d: isinstance(d, dict) and d.get('meta', {}).get('title') == 'x'),
    ("② 多元素里取字段最多的那个（教案本体最大）",
     [{'a': 1}, {'meta': {}, 'knowledge': [1, 2, 3], 'homework': [1, 2]}],
     lambda d: isinstance(d, dict) and 'homework' in d),
    ("③ 本来就是对象，不动它",
     {'a': 1}, lambda d: d == {'a': 1}),
    ("④ 数组里没有对象就原样返回，不瞎猜",
     ['纯文本', '还是纯文本'], lambda d: d == ['纯文本', '还是纯文本']),
]
wrap_ok = 0
for name, payload, check in WRAP_CASES:
    got = _coerce_object(payload)
    good = check(got)
    print(f"  {'✅' if good else '❌'} {name}")
    if not good:
        print(f"       得到：{got!r}")
    wrap_ok += good
print(f"  → {wrap_ok}/{len(WRAP_CASES)} 通过")

# chat_object 是 compose/review/refine/segment 的统一入口：
# 必须保证返回 dict，否则调用方一个 .get 就把整条流水线带崩（实测踩过两次）
print()
print("=" * 74)
print("chat_object：必须保证返回对象，否则如实报错而不是让调用方炸")
print("=" * 74)
from app.llm.client import ModelClient

client = ModelClient.__new__(ModelClient)          # 只借方法，不初始化网络配置
fake = [
    ("返回对象", {"verdict": "pass"}, {}, dict),
    ("单元素数组包对象", [{"verdict": "pass", "score": 9}], {}, dict),
    ("多对象数组取字段最多的", [{"a": 1}, {"a": 1, "b": 2, "c": 3}], {}, dict),
    # 下面这条是实测踩到的：模型把知识点写成纯字符串数组，内容全对但结构不对
    ("纯字符串数组 → 结构重排后拿到对象",
     ["记忆与技巧：记住完全立方、立方和公式", "过程与方法：先凑、再配、再解"],
     {"key_points": ["记忆与技巧：记住完全立方、立方和公式",
                     "过程与方法：先凑、再配、再解"]}, dict),
    ("纯字符串数组且重排也失败 → 如实报错", ["pass", 88], "就一句话", LLMError),
]
obj_ok = 0
for name, payload, repair_payload, want in fake:
    client.chat_json = lambda *a, _p=payload, **k: _p                  # noqa: E731
    client.chat = lambda *a, _p=repair_payload, **k: json.dumps(_p, ensure_ascii=False)  # noqa: E731
    try:
        got = client.chat_object([{"role": "user", "content": "x"}])
        ok = isinstance(got, want) and want is dict
    except LLMError:
        got = "LLMError"
        ok = want is LLMError
    print(f"  {'✅' if ok else '❌'} {name}")
    if not ok:
        print(f"       得到：{got!r}")
    obj_ok += ok
print(f"  → {obj_ok}/{len(fake)} 通过")
wrap_ok = wrap_ok + obj_ok
tot_cases = len(WRAP_CASES) + len(fake)

print()
print("=" * 74)
print("总计：" + ("全部通过 ✅" if passed == len(CASES) and same and wrap_ok == tot_cases else "有失败项 ❌"))
print("=" * 74)