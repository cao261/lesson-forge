# LessonForge · 视频转教案

输入一个 B 站教学视频链接，输出一份**可以直接照着讲**的家教教案（Word）：

- 知识点成体系梳理，每条都能回溯到视频时间戳
- 例题的**题目原图**从视频里截出来，直接插在知识点后面
- 题干由多模态模型**看图转写成 LaTeX**，公式渲染成 **Word 原生公式对象**（可点击编辑，不是贴图）
- 有**审核环节**：规则体检 + 模型交叉审核，不通过自动回炉重写

---

## 一、快速开始

```bash
# 1. 填 API Key
copy .env.example .env      # Windows；macOS/Linux 用 cp
#    然后编辑 .env，把 MINIMAX_API_KEY= 后面的引号内容替换成你的 Key

# 2. 启动
.venv\Scripts\python.exe run.py
```

浏览器打开 `http://127.0.0.1:8760`，粘贴链接 → 点「开始生成教案」。

也可以走命令行：

```bash
.venv\Scripts\python.exe run.py "https://www.bilibili.com/video/BV1xxxxxxxxxx"
.venv\Scripts\python.exe run.py "<链接>" --subject=数学 --grade=高一 --minutes=90 --no-review
```

首次运行会自动下载 Whisper 模型（`large-v3` 约 3GB，走 hf-mirror 镜像），之后就快了。
建议先在 `.env` 里设 `WHISPER_MODEL=medium` 快速试用，效果满意再换回 `large-v3`。

**想要显卡加速（强烈建议）**：如果你的机器只装了显卡驱动、没装 CUDA Toolkit，
转写时会报 `Library cublas64_12.dll is not found`。装一下就行：

```bash
.venv\Scripts\python.exe -m pip install -r requirements-gpu.txt
```

不装也能跑——程序会自动退回 CPU 并改用 int8 精度，只是 1 小时视频要多等 40 分钟以上。

---

## 二、它是怎么工作的

```
B站链接
  │
  ├─① yt-dlp 解析并下载视频（自动合并音视频流）
  │
  ├─② ffmpeg 抽音轨 → 本地 Whisper 转写（词级时间戳）
  │      产出：[00:12] 同学们好，今天我们来讲……
  │
  ├─③ 大模型按字幕切知识点，并标出「老师开始讲第 N 道例题」的那一秒
  │      产出：kp1 00:00–04:20 / ex1 @ 02:15 / ex2 @ 05:40 …
  │
  ├─④ 在例题时间戳前后按 1fps 抽帧
  │      用「画面突变（切页/翻题）」+「画面稳定（写完了）」综合打分
  │      取 top-4 候选帧
  │
  ├─⑤ 多模态模型逐张看图 → 把板书上的题目转成 LaTeX
  │      看不清就标 [看不清]，绝不猜；确认不是题目就换下一张候选
  │      产出：题干 LaTeX + 置信度 + 最终采用的那张截图
  │
  ├─⑥ 助教按「字幕 + 题干」写教案（目标/重难点/知识点/例题解析/流程/作业）
  │
  ├─⑦ 审核：规则体检（本地）+ 模型交叉审核（查编造、查公式错）
  │      不通过 → 带着意见回炉重写，最多 2 轮
  │
  └─⑧ 导出 Word：LaTeX → OMML 真公式 + 例题原图 + 审核记录
```

每一步的产物都落在 `workspace/<链接哈希>/` 里。**改完提示词想重跑时，点界面上的
「改完提示词后重跑」按钮即可**——会复用已下载的视频和已转写的字幕，只重跑后面几段，
一分钟内就能看到新的教案。

---

## 三、目录结构

```
lesson-forge/
├── run.py                    入口（网页界面 / 命令行）
├── .env                      配置（自己填，不要提交）
├── requirements-gpu.txt      可选：GPU 加速依赖
├── app/
│   ├── config.py             所有配置项
│   ├── models.py             教案数据结构（pydantic）
│   ├── pipeline.py           流水线编排 + 中间产物复用
│   ├── llm/
│   │   ├── client.py         模型客户端（双协议 + 主备切换 + 限流）
│   │   ├── routing.py        RPM 限流器 + 错误分类（哪些该换模型）
│   │   ├── prompts.py        ★ 全部提示词，教案质量的核心，想改风格改这里
│   │   └── mock.py           离线假数据模式
│   ├── stages/
│   │   ├── fetch.py          ① 下载
│   │   ├── asr.py            ② Whisper 转写
│   │   ├── segment.py        ③ 知识点切分 + 例题定位
│   │   ├── shots.py          ④ 例题截图 + 板书截图
│   │   ├── vision.py         ⑤ 看图转写题目
│   │   ├── compose.py        ⑥ 生成教案
│   │   ├── review.py         ⑦ 审核 + 回炉
│   │   └── export.py         ⑧ 导出 Word / HTML
│   ├── media/                ffmpeg 封装 + 候选帧打分算法
│   ├── docx/
│   │   ├── omml.py           LaTeX → MathML → OMML（Word 真公式）
│   │   └── builder.py        Word 排版
│   └── web/                  FastAPI + 网页界面
│       └── static/mathjax/  内置 MathJax，离线也能渲染公式预览
├── workspace/                生成结果（每个链接一个目录）
│   └── <hash>/
│       ├── source.mp4        下载的视频
│       ├── audio.wav         抽出的音轨
│       ├── transcript.json   带时间戳的字幕
│       ├── segments.json     知识点切分结果
│       ├── shots/            所有截图
│       ├── lesson.json       结构化教案（可二次加工）
│       ├── 教案.docx          ★ 最终交付物
│       └── 教案.html          浏览器预览
├── selfcheck_docx.py         Word 导出自检（不需要网络和 Key）
├── selfcheck_omml.py         公式转换自检
├── selfcheck_api.py          模型接口连通性自检（文本/JSON/图片）
├── selfcheck_asr.py          Whisper 转写自检
├── selfcheck_fallback.py     主备切换 + 限流自检
└── requirements.txt
```

---

## 四、改成你自己的风格

所有提示词都在 **`app/llm/prompts.py`**，改完直接点界面上的重跑按钮，不用重新下载视频。

| 常量 | 管什么 |
|---|---|
| `PERSONA` | 老师的身份和铁律（严谨、不编造、可落地、讲人话） |
| `SEGMENT_SYSTEM/USER` | 怎么切知识点、怎么给例题打时间戳 |
| `TRANSCRIBE_SYSTEM/USER` | 怎么把板书上的题目转成 LaTeX |
| `COMPOSE_SYSTEM/USER` | 教案的章节结构、每部分写什么、字数多少 |
| `REVIEW_SYSTEM/USER` | 审核清单、pass/revise/fail 的判定标准 |
| `REVISE_SYSTEM/USER` | 回炉时怎么改、改不动的地方怎么处理 |

想让教案更详细就在 `COMPOSE_USER` 的 JSON 结构里加字段，
然后在 `app/models.py` 加同名字段，在 `app/docx/builder.py` 里加对应章节。

---

## 五、模型主备切换（额度用完自动降级）

`.env` 里可以配**多档模型**，按顺序尝试：

```
PRIMARY_NAME=MiniMax-M3          ← 额度够就一直用它
PRIMARY_API_KEY=...
PRIMARY_MODEL=MiniMax-M3
PRIMARY_PROTOCOL=openai          ← openai 或 anthropic
PRIMARY_VISION=1                 ← 是否支持看图

FALLBACK_ENABLED=1
FALLBACK_NAME=agnes-3.0-flash    ← 主模型额度用完时自动接管
FALLBACK_API_KEY=...
FALLBACK_BASE_URL=https://apihub.agnes-ai.com/v1
FALLBACK_MODEL=agnes-3.0-flash
FALLBACK_PROTOCOL=openai
FALLBACK_RPM=10                  ← 每分钟最多几次，程序自动匀速排队
FALLBACK_VISION=1
FALLBACK_ON_OVERLOAD=1           ← 主模型连续过载后也降级
```

**什么时候会自动切换**（由错误内容判断，不是看状态码）：

| 情况 | 处理 |
|---|---|
| `insufficient balance` / `余额不足` / `quota exhausted` | 立刻切备用，不重试 |
| `Invalid token` / 401 / 403 | 立刻切备用 |
| `rate limit` / `请求过于频繁` | 先退避重试，重试用尽再降级 |
| `529 负载较高` / 503 / 超时 | 退避重试，`FALLBACK_ON_OVERLOAD=1` 时重试用尽后降级 |

切���动作会实时打在网页界面的日志里，也会写进最终教案的「审核记录」。

**限速档怎么算时间**：设 `FALLBACK_RPM=10` 就是两次调用之间至少隔 6 秒，匀速发。
故意不做"先连发 10 次再歇一分钟"那种突发桶——平均速率虽然达标，
但会瞬间撞满服务端的滑窗限流然后被拒。一节课大约 25~40 次模型调用，
10 RPM 意味着最坏多等 2~4 分钟，换来的是不中断。

**看图的活儿会挑能看图的模型**：`PRIMARY_VISION=0` 时，例题转写那一步会直接用备用模型，
其余环节照常用主模型。题干转写是整个流程里最不能出错的一步，
如果你的免费模型看不懂图，别把它标成支持。

**加第三档**：目前是主 + 备两档。要更多的话，在 `app/config.py` 的 `providers` 元组里
照着 `_provider(...)` 再加一条即可，路由是通用的。

---

## 六、常见问题

**公式没渲染出来 / 变成方框**
Word 公式依赖 Office 自带的 `MML2OMML.XSL`。程序会在
`C:\Program Files\Microsoft Office\root\Office16\` 下自动探测。
如果没有（装了 WPS 或精简版 Office），在 `.env` 里把 `MML2OMML_XSL`
指向一个 XSL 文件；找不到也没关系，会自动降级成 300DPI 高清公式图片，
只是不能在 Word 里直接编辑公式。

**某道例题标了 ⚠**
说明多模态模型没能在候选帧里确认到完整题干（板书被擦掉了、
题目在翻页时一闪而过、或者老师全程口述没板书）。
这种情况在「审核记录」一节里会列出来，对着视频时间戳看一眼就行。
想提高命中率：调大 `.env` 里的 `SHOT_POST_ROLL`（题面出现得晚时有用）
或 `SHOT_CANDIDATES`（候选帧更多）。

注意有些课的老师是**在题面旁边手写推导**，那种情况下画面上的印刷题干始终完整可读，
只是旁边多了手写内容——这不影响转写，因为提示词要求模型只转写题面本身。

**字幕里冒出繁体字**
Whisper 在中文长音频上有时会中途漂到繁体。程序已经内置繁简统一（`zhconv`），
如果你看到还有残留，多半是模型把某个词整个听错了——把 `.env` 里的
`WHISPER_MODEL` 换成 `large-v3` 会明显改善。

**下载不了 / 提示需要登录**
B 站对未登录用户限制 1080P 以上。在 `.env` 里设置
`BILI_COOKIES_FROM_BROWSER=chrome`（或 `edge` / `firefox`），
程序会直接读你浏览器里的登录态。

**视频太长跑太久**
Whisper 是瓶颈。`.env` 里把 `WHISPER_MODEL` 改成 `medium` 快 3~4 倍，
`small` 快 10 倍，准确率会下降。也可以先用 `VIDEO_QUALITY=720` 减小下载量
（对转写质量几乎没影响，只影响截图清晰度）。

**想先看看效果不想花钱**
`.env` 里设 `LESSONFORGE_MOCK=1`，会用假数据把整条链路跑通，
产物是占位内容，用来验证环境和流程。

---

## 七、环境要求

- Python 3.11+（开发环境：3.13）
- ffmpeg（会自动探测 PATH，winget 装的能直接找到）
- 显存：有独显快很多（`large-v3` 建议 6GB+），没有也行，会自动退回 CPU
- 装了 Microsoft Office 才有原生 Word 公式；否则降级为高清图片