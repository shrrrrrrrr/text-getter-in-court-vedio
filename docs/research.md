# 技术参考与选择 · 2026-10-28

以下为官方公开资料，已核对；产品宣传不视为本项目效果证据。没有逆向豆包、元宝或腾讯会议的消费端协议，也没有声称复制其内部模型。

| 产品/项目与来源 | 核心能力、目标用户 | 本项目可借鉴之处 | 局限与验证要求 |
| --- | --- | --- | --- |
| [腾讯云录音文件识别](https://cloud.tencent.com/document/product/1093/37823)及[结果查询](https://cloud.tencent.com/document/product/1093/37822) | 长录音异步转写、详细结果、说话人分离；面向开发者 | 提交/查询任务分离；会议引擎；关闭语气词与数字规整；保留说话人和时间轴 | 本地数据提交 5 MB；任务 ID 不是跨日期唯一业务 ID。云端传输和计费需显式选择；未实测中文庭审及其重叠完整性 |
| [豆包语音说话人分离实践](https://docs.volcengine.com/docs/doubao-voice/speaker-separation?lang=zh) | 会议多人识别、录音文件/流式场景 | 会议能力需要专门模型及任务模式，区分转写与说话人链路 | 各版本参数和访问条件不同；本版调研参考，未实现豆包适配器，也未调用元宝或豆包客户端 |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | 可本地部署的 Whisper 推理及词时间戳 | 原始 ASR 与后续处理分开，保留 words 和置信信息 | Whisper 本身可能漏词、数字规整、幻觉；不将解码概率当作校准后的正确率。固定了已实测的 1.2.1 与兼容 PyAV 范围 |
| [WhisperX](https://github.com/m-bain/whisperX) | ASR、词对齐与说话人归属工作流 | 用独立时间轴对齐文字与人物，逐环节保存产物 | 官方说明重叠语音处理不足。因此没有把“加了 diarization”当成已还原多人同时说话 |
| [pyannote community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) | 本地声音聚类、说话人分段；提供普通和 exclusive 输出 | 使用普通输出保留重叠区间，再对每个词归属 | 模型访问条款及 CC-BY-4.0 权重许可；普通输出也不等于语音分离或法律身份认证。真实效果未验收 |
| [SpeechBrain SepFormer WHAMR 16k](https://huggingface.co/speechbrain/sepformer-whamr16k) | 双声部语音分离，噪声/混响训练场景 | 只对检测到的短重叠片段运行，分离后分别 ASR，保留原混合结果 | 英语来源训练集不是中文庭审；官方未保证跨数据集效果。独立片段声部顺序不稳定，不自动当作同一个人 |
| [PixIT AMI 联合分离与 diarization](https://huggingface.co/pyannote/speech-separation-ami-1.0) | 联合说话人分离与声源分离，AMI 单远场麦克风 | 后续值得对照的联合管线，可能改善声部分离后的全局身份关联 | 官方卡标注训练使用 pyannote.audio 3.3.2。当前基线是 pyannote 4，不未经兼容验证直接混装或声称可替换 |

首版选择显式 workflow 而不是让通用语言模型直接“看完视频生成笔录”。音频识别、声纹、重叠分离、角色候选、验证各自可观察；角色 Agent 被限制为结构化候选，不能修改 ASR 原文。它最多读取 120 段和四张取样画面，因此不能称为全视频视觉角色识别。

建议对照：本地 ASR 单独、ASR + diarization、+ 双人分离、腾讯云会议引擎、+ 本地角色 Agent。固定输入、音轨、软件/权重版本与人工参考，不把人工改完的文本用来报告自动识别指标。当前无真实庭审测试成绩。
