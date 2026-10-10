"""结构体检：任意 .docx，数原生公式、图片、表格、章节，并抽查题干文字。"""
import re
import sys
import zipfile
from pathlib import Path

from docx import Document

path = Path(sys.argv[1])
doc = Document(path)

# OMML 原生公式对象
with zipfile.ZipFile(path) as z:
    xml = z.read("word/document.xml").decode("utf-8")
    media = [n for n in z.namelist() if n.startswith("word/media/")]

n_omml = len(re.findall(r"<m:oMath[ >]", xml))
n_omml_para = len(re.findall(r"<m:oMathPara[ >]", xml))
n_inline_img = len(re.findall(r"<a:blip", xml))

paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
heads = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading")]

print(f"文件      ：{path.name}")
print(f"大小      ：{path.stat().st_size / 1024:.1f} KB")
print(f"原生公式  ：{n_omml} 个（独立公式段 {n_omml_para} 个）")
print(f"图片      ：{n_inline_img} 处引用 / {len(media)} 个媒体文件")
print(f"表格      ：{len(doc.tables)} 个")
print(f"非空段落  ：{len(paras)} 段")
print(f"章节标题  ：{len(heads)} 个")
print()
print("章节列表：")
for h in heads:
    print(f"  · {h}")

# 题干抽查：找含 < 的行，验证是不是 -2<x<m
print()
print("题干抽查（数学不等式）：")
hits = [p for p in paras if re.search(r"-2\s*<\s*x\s*<\s*m", p)]
for p in hits:
    print(f"  ✅ 命中：{p[:120]}")
if not hits:
    print("  ⚠ 正文段落里没直接命中 '-2<x<m'，可能公式在 OMML 里（正常）")
    omml_text = " ".join(re.findall(r"<m:t[^>]*>([^<]*)</m:t>", xml))
    for frag in re.findall(r".{0,40}-2.{0,40}", omml_text)[:5]:
        print(f"     OMML 片段：{frag!r}")