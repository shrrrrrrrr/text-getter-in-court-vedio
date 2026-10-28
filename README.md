# 庭录 · 庭审视频逐字转写

AI + 法律项目的本地转写工作台。输入只有庭审视频/音频；输出按发言开始时间排列的 Markdown 逐字稿，保留人物编号、角色候选/确认状态、起止时间、重叠发言和核听标记。**不生成会议摘要，不用语言模型润色或补全发言。**

当前版本 `0.1.0` 是可运行的开发版本。已验证本地 ASR、媒体处理与校对导出链路；**未完成真实中文庭审准确率、全角色识别、复杂多人重叠效果验收**。本地说话人模型需要另外下载并接受其访问条款；腾讯云、实验性分离和可选角色 Agent 的实模效果需要配置后验收。详细边界见 [验收记录](docs/verification.md)。

## 快速运行

要求：Python 3.11–3.13、FFmpeg/ffprobe。前端没有 Node 构建依赖。默认只监听本机，数据保存在忽略的 `.state/`。

以下命令在**本机 PowerShell**、本仓库目录执行。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[local,test]"
```

第一条创建独立 Python 环境；第二条安装可编辑源码、本地 ASR 及测试依赖。首次安装需要网络，预期出现 `Successfully installed`。`av<17` 用于兼容已验证的 faster-whisper 1.2.1，避免 PyAV 19 的 `metadata_errors` 参数错误。

```powershell
ffmpeg -version
ffprobe -version
```

两条应输出版本号；否则先安装 FFmpeg 并加入 PATH，再开启 PowerShell。

```powershell
.\.venv\Scripts\python.exe tools/download_model.py --repo Systran/faster-whisper-small
Copy-Item .env.example .env
```

下载器把公开权重放进 `.state/models/`，输出 `COURT_ASR_MODEL=...` 的实际快照路径。将此行填入本地 `.env`，保留 `COURT_ALLOW_MODEL_DOWNLOAD=0` 即可离线加载。默认 `small` 适合先检验链路；中文庭审质量应对比 `medium`、`large-v3`，不要把 tiny/small 的测试当作质量验收。下载较大的模型可将仓库名改为 `Systran/faster-whisper-large-v3`。正式测评用 `--revision <实际 commit SHA>`，本地快照目录的文件 SHA256 会写入运行清单。

```powershell
.\run.ps1
```

启动地址：[http://127.0.0.1:8766](http://127.0.0.1:8766)。`-Port 8767` 更改端口；`-Reload` 用于开发热重载，重启会把未完成任务标为中断，避免自动重复执行。脚本从 `.env` 读取配置，Ctrl+C 停止服务。Linux/macOS 可用 `.venv/bin/python -m uvicorn court_transcript.app:app --host 127.0.0.1 --port 8766`，环境变量需自行设置。

只体验界面可先点击“打开合成演示”，不需要识别模型。演示是人工编写的合成内容，没有真实声轨，绝不会作为识别失败后的回退结果。仅安装 ASR 时，上传框默认关闭说话人分离，所有声音保持未分配。

## 人物、重叠发言与画面

准备部署到校园服务器时，可使用[一次执行的环境准备脚本](docs/campus-prepare.md)，批量完成资源检查、固定版 large-v3 下载、隔离镜像构建和 GPU 短测试。该脚本不开放网站、不修改现有 GLM；通过后再配置校园网多人访问。

### 本地说话人分离

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[diarization]"
```

安装 pyannote.audio 4。其 `community-1` 模型有独立权重访问要求：在 [模型页](https://huggingface.co/pyannote/speaker-diarization-community-1) 接受条款，将访问令牌只放本地 `.env` 的 `HF_TOKEN`。首次下载设置 `COURT_ALLOW_MODEL_DOWNLOAD=1`；下载后关闭下载开关，并可将 `COURT_DIARIZATION_MODEL` 指向完整本地管线。重启工作台，在任务中启用说话人分离。依赖安装成功不等于模型能加载，任务会报告实际失败。

使用**普通 diarization** 保留同时发声信息，不使用丢弃重叠的 exclusive 输出。每个识别词只归属一次，归属不确定会标记；检测到另一个声部却没有原文时输出 `[重叠发言，内容待核听]`。占位不是识别成功。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[overlap]"
```

可选安装 SpeechBrain SepFormer 双人语音分离。需下载权重，在任务中显式勾选实验功能。仅尝试 **2 人、单个重叠区间 ≤15 秒**；3 人以上或长重叠保留待核听项。独立分离声轨标为 `OVL_区间_声部`，**不能认为它们就是不同的新人物，也不能自动映射到原人物**。在人物区新增/确认人物，在逐句校对中重新分配。原混合识别保存在私有 `mixture-alternatives.json`，两条声部均产生文本才替换相应重叠区间。模型训练集不是中文庭审，结果须核听，见 [调研](docs/research.md)。

### 角色与本地角色分析 Agent

默认用“我是原告”“本人系书记员”等自述线索产生角色候选，不把“法官您好”误认为说话人是法官。角色支持自定义；默认列表覆盖法官、审判长、人民陪审员、书记员、当事人、公诉人、辩护人、代理人、证人、鉴定人、翻译、公证人、旁听人员等。选择须符合具体法域与案件，不预设每类角色都会出庭或发声。**无声音的人没有发言可转写；同一声纹不等于已核实法律身份。**

可选配置仅限本机的兼容接口：

```dotenv
COURT_ROLE_API_BASE=http://127.0.0.1:8000/v1
COURT_ROLE_MODEL=your-local-model-id
COURT_ROLE_API_KEY=
```

配置后重启，在任务中选择角色 Agent。它只提出角色候选，不改原文。所有引用必须是已有片段的精确子串，并属于对应声音；确认状态始终留给人工。可勾选抽帧输入供**支持图片的模型**分析桌牌等线索；最多取四个声音首段附近的画面，保留抽帧时刻和提示词指纹。这是采样辅助，**没有做全视频人物跟踪、音画口型匹配或人脸身份认证**。上下文最多取 120 段，取样清单写入 manifest；失败仍保留转写与规则候选。

### 腾讯云会议识别（可选）

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[tencent]"
```

在 `.env` 配置 `TENCENTCLOUD_SECRET_ID`、`TENCENTCLOUD_SECRET_KEY`。每次任务明确选择腾讯云并同意传输音频后才调用。采用官方 `CreateRecTask` / `DescribeTaskStatus` 与 `16k_zh_en_meeting`，开启说话人分离，关闭语气词、脏词过滤及数字规整；不使用口语转书面语。当前经本地压缩音频提交，官方限制为 **5 MB**，本适配器超限拒绝，不自动切片造成全庭人物编号混乱；长视频优先本地路线。

提交前持久化检查点，提交结果未知时阻止自动重复提交；取得云端任务 ID 后恢复只查询原任务。本地取消不撤销云端任务或费用。未实际调用付费云服务，不能声称已通过云端效果验收。参考的是腾讯云开放语音服务，不是直接调用腾讯会议/元宝消费端接口。

## 校对与导出

1. 选择一段视频、语言、音轨（从 0 开始）和处理方式。任务持久化，页面关闭后服务仍处理；应用进程停止则中断。
2. 点击发言时间回听音轨。需要画面时生成 H.264/AAC 通用回放副本，或查看当前时刻抽帧；原视频不覆盖。
3. 搜索原话，筛选人物、待核听或重叠片段。逐句修改文本、时间和人物，核对后勾选“已回听”。
4. 在人物卡设置角色与证据。确认身份必须记录证据片段或人工说明。原始文字不可通过校对接口覆盖。
5. 导出 Markdown（按起始时间排序）、JSON（原文、词时间戳、运行清单）和修改记录。未核听/候选/重叠状态不会因导出被隐藏。

数据文件：`input.media` 原文件；`audio.wav` 原音轨规范化；`preview.mp4` 回放副本；`asr-raw.json` 原 ASR；`diarization.json` 声音区间；`original-result.json` 初始结果；`task.json` 当前版本；`audit.jsonl` 人工修改链。修改记录含前后内容和串联 hash，**是本地可检查记录，不是防管理员篡改的法定证据系统**。

支持常见 MP4、MKV、MOV、AVI、WebM、FLV、WMV、M4V、MPEG、TS/MTS/M2TS、3GP、VOB、RM/RMVB、OGV，以及 WAV、MP3、M4A、AAC、FLAC、OGG、OPUS、AMR、WMA、AIFF、CAF、APE 等扩展名。**具体编码、损坏情况、DRM、FFmpeg 编译能力仍决定是否可解码**，不保证每种编码组合。入口先用 ffprobe 实测音轨，不按扩展名假设成功。拒绝播放列表、网络协议、任意 URL 和无音轨文件。默认 4 GB / 8 小时，可在 `Settings` 中修改；长视频应确认磁盘、CPU/GPU 和内存余量。

## 验证与版本管理

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .state/pytest-local-01
node --check static/app.js
```

第一条运行逻辑、接口、任务及真实 FFmpeg 容器测试；临时目录应使用专用新目录。第二条检查浏览器脚本语法。真实浏览器验收可安装 Playwright 并运行 `node tools/browser_verify.mjs`，`PLAYWRIGHT_MODULE` 可指定已有模块，`PLAYWRIGHT_EXECUTABLE_PATH` 指定已有 Chromium。测试只使用合成数据，截图及报告在 `.state/browser/`。

专家标注与机器导出采用同样 `segments` 字段，可执行：

```powershell
.\.venv\Scripts\python.exe tools/evaluate.py reference.json prediction.json --speaker-map speaker-map.json
```

显式映射预测声音 ID 到参考人物 ID。输出 CER、人工映射后按人物汇总的 CER 和重叠时长 precision/recall；未映射声音算错。此小工具不是完整 DER/cpWER 实现，也不能代替法律角色准确率验收。标注要求见 [验收规格](docs/spec.md)。

公开前运行 `git diff --cached` 人工检查和 `python tools/check_public_tree.py`。视频、权重、配置、日志、测试截图和真实结果都只留 `.state/`，不进入公开仓库。

## 设计与后续接入

沿用项目内法律工具工作台的奶油纸面、海军蓝、青绿、ChillReunion Round 真实字体与轻量像素几何，参考 `frontend-design`、`pbl-pixel-course` 和已有工作台设计记录；字体 OFL 许可随附。详见 [设计说明](docs/design.md)。

本版只在本机运行，没有改动或部署到法律站、SimCourt 或共享模型平台。未来公网接入前需要成员鉴权、任务/视频所有权、配额、上传隔离与沙箱、持久 worker、存储清理和真实样本评测，不能将此单人版本直接绑定公网地址开放。
