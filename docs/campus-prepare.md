# 校园服务器批量准备：一次执行

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
