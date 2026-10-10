"""请求体风格自检：同一段内容，不同厂商要的字段名不一样，别发错。"""
from app.config import SETTINGS
from app.llm.client import ModelClient

CASES = [
    ("MiniMax-M3", "https://api.minimaxi.com/v1", "openai"),
    ("agnes-3.0-flash", "https://api.agnes-ai.cn/v1", "openai"),
    ("some-claude", "https://example.com/v1", "anthropic"),
]

MSGS = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "只输出 JSON：{\"a\":1}"},
]

print("=" * 70)
print("请求体风格自动识别 + 字段适配")
print("=" * 70)
c = ModelClient()
for name, base, proto in CASES:
    p = {"name": name, "model": name, "base_url": base, "protocol": proto,
         "style": "anthropic" if proto == "anthropic" else ("minimax" if "minimax" in base else "openai"),
         "rpm": 0, "vision": True}
    out = c._build_payload(p, MSGS, temperature=0.2, max_tokens=32000,
                           json_mode=True, thinking="adaptive")
    print(f"\n【{name}】  protocol={proto}  style={p['style']}")
    for k, v in out.items():
        if k == "messages":
            print(f"    {k}: <{len(v)} 条消息>")
        else:
            print(f"    {k}: {str(v)[:70]}")

print()
print("=" * 70)
print("关键差异")
print("=" * 70)
print("  MiniMax  → max_completion_tokens + thinking.type + response_format")
print("  Agnes/标准→ max_tokens + chat_template_kwargs.enable_thinking，不发 response_format")
print("  Anthropic→ max_tokens + system 独立字段 + thinking.type")
print()
print("当前真实配置：")
for p in SETTINGS.providers:
    print(f"  {p['name']:18s} protocol={p['protocol']:10s} style={p['style']:10s} rpm={p['rpm']}")