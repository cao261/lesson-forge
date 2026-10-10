# LessonForge · 视频转家教教案

粘贴一段 B 站教学视频，产出一份**能直接照着讲**的 Word 教案。

下载 → 本地语音转写 → 切分知识点 → 截取例题画面 → 多模态读图还原题干
→ 生成教案 → **两道关审核** → 导出两份 Word（教师备课版 / 学生讲义）。

## 它和"把字幕丢给大模型"差在哪

| 常见做法 | 这个项目 |
|---|---|
| 用视频自带字幕 | **一律本地 Whisper**，视频没字幕也能做 |
| 例题靠模型猜 | 截**真实画面**，多帧候选 + 多模态逐张确认 |
| 公式贴截图 | **Word 原生公式对象（OMML）**，点开就能改 |
| 出了事才知道 | **审核前置**：规则体检 + 模型交叉核查，不通过就回炉重写 |
| 一份文档 | 教师备课版 + 学生讲义两份，出自同一份数据，不会对不上 |

## 快速开始

```bash
# 1. 填 API Key
copy .env.example .env       # Windows；macOS/Linux 用 cp
#    编辑 .env，把 PRIMARY_API_KEY 换成你的 Key

# 2. 装依赖
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 3. 启动
.venv\Scripts\python.exe run.py
```

浏览器打开 `http://127.0.0.1:8760`，粘贴链接 → 点「开始生成教案」。

后台启动不弹浏览器：先设 `LESSONFORGE_NO_BROWSER=1`。

## 输出

两份 Word，出自同一份数据：

- **教师备课版**：教学目标、重难点、逐环节逐句的教师话术、易错点
- **学生讲义**：知识点讲解 + 例题原图 + 分步解析

公式是 Word 原生 OMML 对象，可点击编辑；例题贴视频里的**原始截图**。

## 几个关键设计

**公式必须原生**。LaTeX 经 MML2OMML.XSL 转成 OMML，不贴图——贴图就没法改了。

**审核分两类**。`fact`（事实错误）与 `supplement`（教学补充）分开：只有前者算编造、
驱动回炉；后者单列成建议。把补充意见喂回模型会让它越改越乱。

**回炉会早停**。事实错误数没降（≥90%）就停，不无限重试，并在结论里如实说明。

**断点保存**。compose 草稿与每轮回炉结果都落盘，中途失败不用从头跑。

**并发有据可依**。并发上限按批次数量定（默认 6），不是按 RPM；限速器按 provider
全局共享，否则 N 个客户端各放 10 RPM 会叠着超限。

## 配置

常用环境变量（都写在 `.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `PRIMARY_API_KEY` | — | 主模型密钥，必填 |
| `PRIMARY_BASE_URL` | MiniMax | 换成任意 OpenAI 兼容端点 |
| `FALLBACK_API_KEY` | — | 备用模型，额度用完自动降级 |
| `LLM_PARALLEL` | 6 | 并发批次数 |
| `LLM_STREAM` | 1 | 流式收发（**别关**，见下） |
| `LLM_READ_TIMEOUT` | 300 | 流式读超时（多久没收到帧才算断） |
| `MAX_REVIEW_ROUNDS` | 2 | 回炉轮数上限 |
| `WHISPER_MODEL` | large-v3 | 本地转写模型 |

### 为什么必须开流式

客户端默认 `LLM_STREAM=1`，这不是优化是必需。

主模型开思考后会沉默很久才吐第一个字节；非流式时上游一个字节都不发，
链路中段的网关按空闲超时把连接掐掉，表现成两个服务商都固定在
~128 秒报 `Server disconnected`。改成流式后，读超时按"多久没收到帧"算，
思考几分钟也不断。

同理，`app/llm/client.py` 里处理 `<think>` 的逻辑也不能简化：思考正文里全是
LaTeX 花括号，`<think>` 未闭合时若不整段切掉，括号扫描会从思考里的第一个
`{` 开始乱扫，抓出一个碰巧合法但只有 `meta` 四个字段的片段——那就是空壳教案
的来源，且飘忽不定。

## 自检

改动过核心逻辑后跑一遍：

```bash
.venv\Scripts\python.exe selfcheck_think_json.py        # think 剥离与 JSON 提取
.venv\Scripts\python.exe selfcheck_compose_snapshot.py  # 中途存档不影响主流程
.venv\Scripts\python.exe selfcheck_draft_cache.py      # 断点草稿
.venv\Scripts\python.exe selfcheck_review_batch.py      # 审核分批与分级
.venv\Scripts\python.exe selfcheck_normalize.py        # 切分后结构体检
```

## 目录

```
app/
  config.py        全局配置（frozen dataclass，改配置须在导入前设环境变量）
  pipeline.py      阶段编排、断点保存
  llm/             模型客户端：流式、主备降级、限流、并发批处理
  stages/          fetch / transcribe / segment / compose / review / revise
  docx/            OMML 公式与文档生成
  web/             本地网页界面
run.py             启动入口
```

## 说明

- 字幕一律本地 Whisper（默认 `large-v3`），视频不联网到任何转写服务。
- 例题截图来自视频原帧，题干由多模态模型读图转写成 LaTeX。
- 生成的教案是**备课辅助材料**，正式上课前请自行核对公式与结论。