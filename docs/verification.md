# v0.1.0 验证记录 · 2026-10-28

## 已执行的验证

- Python/接口/任务回归：32 项通过，包括原文保留、乐观版本冲突、角色证据、Agent 引用校验与仅本机地址限制、任务恢复、审计 hash、云端重叠输出解析和基础评测脚本。
- 真实 FFmpeg：MP4、MKV、MOV、AVI、WebM、FLV 六种合成视频容器提取为 16 kHz 单声道 PCM；MKV 转 H.264/AAC 回放与抽帧；无音轨、损坏文件和伪装播放列表失败路径通过。未逐一验证所有列出的扩展名与编码组合。
- 真实 Chromium：1440×1000 校对保存、确认角色、搜索、重叠筛选、Markdown 下载；1920×1080、1024×768、768×1024、390×844、360×740 无横向溢出；桌面工作区分区滚动，无整页垂直溢出；无浏览器 JavaScript 错误。
- 本地真实 ASR 冒烟：Windows 自带英文 TTS 生成完全合成录音，公开 faster-whisper tiny 固定快照 `d90ca5fe260221311c53c58e660288d3deb8d356`、CPU/int8、词级时间戳，成功输出原话，包括重复 “No, no”。该模型将读出的数字识别为阿拉伯数字，说明应用不润色不等于模型绝不规整数字。
- 真实端到端：合成英文录音封装为 MKV，经实际 HTTP 上传、探测、规范化、ASR、词级时间戳、权重文件指纹、未知角色、Markdown/JSON/原始结果导出、视频抽帧及回放转换。输出两段发言，该次 worker 处理记录约 2.6 秒，不含上传，不可外推长庭审速度。

测试命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .state/pytest-release
node --check static/app.js
node tools/browser_verify.mjs
.\.venv\Scripts\python.exe tools/smoke_e2e.py .state/speech-smoke.mkv
```

浏览器脚本需本机 Playwright/Chromium 和已启动的服务；端到端脚本需本地模型和调用者明确提供的合成视频。私有报告、模型、音视频与截图只在 `.state/`，不随仓库发布。

首次真实音频测试发现 faster-whisper 1.2.1 对 PyAV 19 的 `metadata_errors` 参数不兼容，已约束 `av>=14,<17` 并用 16.1.0 重跑成功。测试框架对当前环境的 httpx/anyio 有两条弃用警告，当前 32 项测试通过，未把警告当作识别质量信息。

## 未完成的效果验收

未提供真实授权中文庭审视频。未安装/实测受访问条款约束的 pyannote 权重；未实模验证 SepFormer 的中文双人重叠效果；未配置腾讯云凭证或发起付费调用；未配置本地视觉/文本角色模型。后面这些适配器有实现及部分协议/约束测试，**不得称为实模或真实庭审已通过**。

本版没有全视频视觉人物跟踪、音画说话人匹配、自动识别全部无自述角色或三人以上重叠完整恢复。可选角色 Agent 只做取样证据候选，人工校对才能确认角色。正式质量目标与专家标注方案见 spec.md；部署法律站尚未进行。

## 校园批量准备脚本增量

新增 `tools/campus_prepare.py`：固定公开模型/镜像来源，正常 TLS 下载、缓存复用、GPU 空闲门禁、隔离构建与无网络的单 GPU 控制录音测试；仅清理唯一命名且正确标记的自有测试容器。新增 13 项本地测试通过，包括下载 SHA/大小、现有文件不覆盖、来源间部分下载隔离、GPU忙碌拒绝、容器边界、清理归属、拒绝环境变量中的远程 Docker 上下文和模拟全流程；与原测试合计 45 项。模拟依赖不会运行真实 Docker 或 GPU。

服务器尚未执行该批量脚本，因此不能声称权重已在服务器下载、GPU large-v3 已通过或网站已经上线。实际成功须以用户返回 `COURT_ASR_PREPARE_OK` 及其真实报告为证。准备范围见 campus-prepare.md。

## curl 60 后的离线替代验证

用户实际运行固定版启动块，在首条 GitHub `curl` 请求得到 60 / self-signed certificate in certificate chain；由 `set -e` 可知后续 sudo 安装未执行。未确认具体证书发行者、代理配置或完整信任链，不把症状直接归因为某张校园 CA，不关闭校验、不修改系统信任。

新增完整离线方案：正常 TLS 在本机下载并逐一核对模型、控制录音、Linux wheel 官方 SHA256，生成约 4.47 GB 的私有归档；PowerShell 自动执行 SSH 前置检查、SCP、归档校验、安全解包、服务器专用 venv 安装及无网络的短测试。新增 13 项回归通过，总计 58 项；覆盖目标 Linux/Python 环境标记（实际补齐 Python 3.10 的 exceptiongroup）、固定 Hub 版本、无索引/无网络安装、临时服务归属、损坏包拒绝、路径穿越/链接拒绝及模拟全流程。PowerShell 入口语法通过；远端 Python 3.10 语法检查通过。真实校园安装仍待用户执行，不能用这些结果代替实机 GPU/中文庭审效果验收。
