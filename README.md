# LessonForge · 视频转教案

把一段家教/网课视频，变成一份**能直接照着讲**的 Word 教案。

粘贴 B 站链接，系统自动完成：下载 → 本地语音转写 → 切分知识点 → 截取例题画面 →
多模态读图还原题干 → 生成教案 → **两道关审核** → 导出两份 Word。

---

## 它和"把字幕丢给大模型"差在哪

| 常见做法 | 这个项目 |
|---|---|
| 用视频自带字幕 | **一律本地 Whisper**，视频没有字幕也能做 |
| 例题靠模型猜 | 截**真实画面**，多帧候选 + 多模态逐张确认 |
| 公式贴截图 | **Word 原生公式对象（OMML）**，点开就能改 |
| 出了事才知道 | **审核前置**：规则体检 + 模型交叉核查，不通过就回炉重写 |
| 一份文档 | **教师备课版 + 学生讲义**两份，出自同一份数据，不会对不上 |

## 快速开始

```bat
:: 1. 装依赖（只需一次）
setup.bat

:: 2. 在 .env 里填 MiniMax 的 API Key（首次运行会自动生成 .env 并打开记事本）

:: 3. 启动，浏览器会自动打开
start.bat
```

也可以走命令行：

```bat
python run.py "https://www.bilibili.com/video/BVxxxx"        :: 单条
python run.py --batch links.txt                              :: 批量，一行一个链接
python run.py <链接> --subject=数学 --grade=高一 --minutes=90 :: 指定参数
```

## 输出

```
workspace/<任务ID>/
├─ 教案·教师备课版.docx      讲解话术、审核提示、易错点、板书截图
├─ 教案·学生讲义.docx        精简投影，只有题目和要点
├─ 教案·教师备课版.html      浏览器快速预览（MathJax 渲染公式）
├─ source.mp4 / audio.wav    素材缓存，同一链接重跑直接复用
├─ transcript.json           转写结果
├─ segments.json             知识点切分
└─ lesson.draft.json         教案断点，中途失败可续跑
```

## 设计上的几条硬规矩

- **宁可交有缺口的教案，不交内容编造的教案。** 视频里老师只报了答案没讲过程，
  那份教案就只写答案，绝不编一套解析出来。
- **审核宁可标出来让人判断，也不替用户删。** 自动删标签实测误报率三分之一。
- **审核分两类**：与视频对不上的**事实错误**才算编造；口诀、作业题这类老师
  自己会补的内容算**教学补充**，不算错、不进回炉。
- **只能删确定的。** 相似度高且时间大幅重叠的重复例题才自动删；"整题 vs 它
  的某一道小题"这种只报出来让人确认。

## 环境要求

- Python 3.11+
- FFmpeg（转码/抽帧用）
- Word 的 `MML2OMML.XSL`（用于生成原生公式，通常随 Office 一起装）

NVIDIA 显卡可选，装上之后语音转写走 GPU：

```bat
pip install -r requirements-gpu.txt
```

## 自检

每个改动都配了自检脚本，直接跑：

```bat
python selfcheck_omml.py          :: 公式是不是真的 OMML 对象
python selfcheck_parallel.py      :: 并发执行器：保序、异常隔离、限速共享
python selfcheck_normalize.py     :: 结构体检：去重、裁时间、不误删
python selfcheck_draft_cache.py   :: 断点保存与停滞早停
python selfcheck_review_batch.py  :: 审核分批与报文裁剪
python selfcheck_refine_patch.py  :: 回炉补丁的三级兜底链
python selfcheck_tidy_title.py    :: 标题清洗
python selfcheck_fetch_reuse.py   :: 素材复用不发网络请求
```

## 目录

```
app/
├─ pipeline.py      流水线编排（下载→转写→切分→截图→读图→成稿→审核→导出）
├─ config.py        配置，全部可用环境变量覆盖
├─ textnorm.py      标题清洗
├─ llm/             模型客户端、路由降级、并发执行、提示词
├─ stages/          fetch / asr / segment / shots / vision / compose / normalize / review / export
├─ docx/            Word 构建、LaTeX→OMML、文本清洗
└─ web/             本地网页界面
```

## 说明

- 字幕一律本地转写，不使用平台 CC
- 例题画面走多模态验证，不做 OCR
- 公式优先 OMML，失败才降级成 300DPI 位图
- 素材默认保留以便重跑复用；删掉整个 `workspace/<任务ID>/` 或只删
  `source.mp4` + `audio.wav` 都可以