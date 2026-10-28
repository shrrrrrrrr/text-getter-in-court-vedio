# 校园服务器批量准备：一次执行

**当前推荐：本机准备、普通 SSH 上传、服务器离线安装。** 用户实际执行旧启动块时，第一条 GitHub HTTPS 请求返回 curl 60（证书链无法验证），因此旧脚本并未运行。随后离线包校验和 SSH 前置检查通过，但 SCP 上传连接关闭。具体 TLS/SCP 断开原因尚未核实；传输现改为普通 SSH，不依赖 SCP/SFTP，不根据报错安装未知 CA，也不使用 `-k`。旧 Docker 联网准备路线保留供正常联网环境使用。

## 离线路线（无需服务器访问互联网）

本机 PowerShell 执行 `tools/deploy_campus_offline.ps1 -Target <普通账号@服务器>`；真实目标只放忽略的私有启动文件，不写公开源码。脚本通过已校验的 SSH 主机密钥连接服务器，自动检测 Python 版本、系统依赖、磁盘，并创建或复用带归属标记的私有上传目录。它不接受明文密码参数，不改 SSH 信任策略。

`tools/build_campus_offline.py` 在本机通过正常 TLS 下载固定模型、官方短控制录音、对应 Linux/Python 的 wheel 依赖，逐一与发布源 SHA256 核对。按目标 Linux 的 Python 版本与平台计算依赖闭包，避免 Windows 主机的环境标记导致漏掉 `exceptiongroup` 等包。Hugging Face Hub 固定到已验证的 1.30.0，忽略缓存里无关的新主版本。模型与依赖只存 `.state/`，归档附文件大小、SHA256、Python ABI 与来源信息，不发布真实归档。

`tools/ssh_bundle_upload.py` 通过普通 SSH 的标准输入传输原始二进制，密码仍由 SSH 在终端中提示。目录和归档名固定于目标摘要及文件摘要；服务器返回已有长度及前缀 SHA256，本机确认前缀一致后才续传。传输结束双方核对完整摘要，只有 `SSH_UPLOAD_VERIFIED` 后才启动解包接收器。不会通过 PowerShell 文本管道传输归档，不调用 SCP/SFTP；链接、硬链接、非本人文件及不匹配前缀会被拒绝，保留原文件。

上传后，解包接收器再次验证归档 SHA256，拒绝绝对路径、路径穿越、链接、重复项和超大成员，随后再执行已校验的离线安装器。离线安装器沿用专用运行目录，在其中创建 Python 虚拟环境，通过已验证的 pip wheel 引导，以 `--no-index --no-deps --disable-pip-version-check` 安装完整本地 wheel 集并执行 `pip check`。不安装宿主全局包，不修改系统 CA、不改 Docker daemon、不拉镜像、不改变 GLM 环境。

短测试使用唯一命名的 systemd 临时服务，以普通账号 UID 运行，设置 `PrivateNetwork=yes`、只读系统/家目录、只允许报告目录写入、12 GiB 宿主内存和 8 CPU 配额。通过选中 GPU 的 UUID 设置 `CUDA_VISIBLE_DEVICES`，测试代码验证仅看到一张 GPU；这是运行选择，**不是 GPU 显存硬配额或内核级设备隔离**。结束只清理描述标记完全一致的自有临时服务，不停止 GLM。复核显存并输出 `COURT_ASR_PREPARE_OK` 才能证明实机短测试通过。

本机已准备好的归档会复用；上传中断不会运行安装器。再次执行同一入口会校验已传前缀并续传，完整包也可跳过数据重传。旧版 SCP 的随机上传目录保留，不自动合并未知文件。安装失败时归档、展开目录及已校验缓存保留，不自动清理用户文件。初次准备与上传期间保持 PowerShell 窗口，密码仅在正常 SSH/sudo 终端提示中输入。

本阶段把分散的检查、模型下载、环境构建和 GPU 冒烟测试合并。当前目标是校园网 IP + 端口；服务器常驻模型以用户最新确认为 GLM，不沿用旧 Qwen 状态。真实主机、IP、GPU 快照和下载记录只保存在忽略目录。

## 本轮脚本做什么

`tools/campus_prepare.py` 是 Linux x86_64 / Python 3.10+ 标准库脚本。由普通 SSH 用户通过 `sudo python3` 执行，工作目录固定为该用户家目录下新建的 `court-transcript-runtime-v1`。它不会在宿主机安装 Python 包，不修改 Docker daemon、现有服务、模型配置、GLM 路由或防火墙。

一次顺序执行：

1. 检查运行身份、专用目录标记与进程锁，拒绝覆盖不认识的现有目录。
2. 检查当前磁盘至少 48 GiB 可用、宿主 RAM 至少 20 GiB 可用、NVIDIA runtime、选中 GPU 的 UUID/当前显存/利用率。默认索引 3；显存占用超过 1 GiB、剩余不足 12 GiB 或利用率高于 5% 时停止，不改用其他 GPU、不停止其他模型。
3. 下载固定版本 Whisper large-v3 五个运行文件，约 2.88 GiB。优先 ModelScope，正常 TLS 失败时尝试 HF；所有文件的大小和 SHA256 均固定，与 SYSTRAN 原始 HF 快照交叉核对。不关闭证书检查、不导入未知 CA。
4. 下载 faster-whisper 官方仓库固定版本的短英文控制录音；不上传用户素材。
5. 以固定 Python 基础镜像摘要构建专用 ASR 镜像，依赖只装在镜像里。固定 faster-whisper 1.2.1、PyAV 16.1.0、CTranslate2 4.8.4、CUDA 12 的 cuBLAS 与 cuDNN 9 包；记录生成的镜像 ID 和构建内容指纹。
6. 再次检查选中 GPU 的 UUID 与空闲状态。测试容器只看到该卡，以普通用户 UID 运行，没有网络、没有发布端口、没有 Docker socket、只读模型和录音挂载；限制为 8 CPU、12 GiB 宿主内存。这些限制**不是 GPU 显存硬配额**。
7. 单任务执行 `int8_float16` 转写、检查词时间戳和公开控制样本的基本识别结果。约每 0.5 秒采样选中 GPU 显存，采样可能漏掉短峰值，也不等同于独占资源证明。
8. 测试结束后仅清理带本项目正确标记、唯一名称的测试容器，观察显存结果。模型与构建缓存保留，重复执行会复用经过校验的文件。

输出 `COURT_ASR_PREPARE_OK` 才表示该次服务器准备和短 ASR 测试通过；本机的 mock 测试不能代替它。失败显示 `COURT_ASR_PREPARE_STOP` 与原因，缓存保留，不盲目重复下载或修改其他服务。执行期间保持 SSH 窗口，完成后无需常驻该窗口。

## 如何执行

聊天中提供的启动块使用**已提交的固定 commit URL 和脚本 SHA256**下载脚本，校验通过后才交给 `sudo python3` 执行。不要改成直接执行未经校验的 `curl | bash`，不要为了下载失败使用 `-k`。`sudo` 密码只在自己的终端输入，不回传。

首次下载和构建可能需要较长时间，取决于校园网络及镜像源。最终只需回传结尾摘要；若失败，回传停止原因和附近日志，勿粘贴密码或凭证。也可在自己的服务器查看 `court-transcript-runtime-v1/status.json`，这份真实运行记录不要提交公开仓库。

## 边界

Whisper large-v3 是在已有转写适配器上验证服务器 GPU 环境的工程基线，不代表已经战胜 Qwen3-ASR/Fun-ASR/GLM-ASR，不代表已通过中文庭审、说话人、角色或重叠发言质量验收。

这轮不开放网站入口，不连接 GLM，不配置受访问条件约束的说话人模型。网站多人访问仍需任务所有权和鉴权；正式 worker 需要按需进程退出/空闲卸载，不能把当前缓存模型的单人进程直接当作已实现的多用户服务。本阶段测试进程退出会释放其自身 CUDA 上下文，没有永久独占 GPU 的配置。

## 固定来源

- [SYSTRAN Whisper large-v3](https://huggingface.co/Systran/faster-whisper-large-v3/tree/edaa852ec7e145841d8ffdb056a99866b5f0a478)
- [ModelScope 同字节镜像](https://modelscope.cn/models/pengzhendong/faster-whisper-large-v3/files)，运行 URL 使用固定文件 revision `792d4a7900e25db2618b6f0655fad2ec9b41df7c`
- [官方短测试录音](https://github.com/SYSTRAN/faster-whisper/blob/9e472ae9de8bb374552f531f752654554b76b2d6/tests/data/jfk.flac)
- [NVIDIA 容器 GPU 设备选择](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/docker-specialized.html)

基础镜像 `python:3.12-slim-bookworm` 的 Linux amd64 manifest 摘要、两个 CUDA Python 包版本及所有模型 SHA256 于 2026-10-28 通过正常 HTTPS 只读核对；具体值在脚本中冻结。脚本不会自动升级这些固定来源。
