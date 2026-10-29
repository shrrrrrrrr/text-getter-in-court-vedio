# 校园 Web 离线部署 · 0.2.0

前提是已存在通过 `COURT_ASR_PREPARE_OK` 的服务器 ASR 环境。本包只包含应用、Web wheel、FFmpeg 和私有初始化凭证，不包含 ASR/CUDA 权重。默认本机单人模式保持可用；校园模式通过 `COURT_ACCESS_DB` 显式启用。

当前目标服务器已完成安装、正式服务沙箱内 GPU 视频闭环和校园监听，开发电脑访问也已实测通过。无需重跑本次安装；以下步骤用于复现或明确的新环境部署。更新已有活跃服务需另行安排，安装器不会直接覆盖运行中的任务。

## 两段操作

1. **本机 PowerShell**：开发者运行 `tools/build_campus_web.py`，通过正常 TLS 下载与官方发布摘要校验，生成 `.state/web-bundles/` 下的增量包及 `.state/deploy/campus-access.local.json`。随后运行 `tools/deploy_campus_web.ps1 -Target <普通账号@服务器> -Listen <校园IPv4> -Port 8766`。入口只校验已通过的 ASR 状态，用普通 SSH 断点续传新包，输出服务器安装块。不会再走已失败的 SCP/SFTP 或远程交互 SSH 启动方式。保留此窗口：上传后它会等待安装完成，并自动从本机直接检查校园 HTTP 会话入口，不发送个人凭证。
2. **原有服务器 Bash 终端**：粘贴入口输出的完整安装块。块内先核对归档 SHA256，再解包并通过正常 sudo 提示执行安装器。不要在本机 PowerShell 中执行 Bash 块。

实际主机、监听 IP、真实凭证、归档和执行结果只保存在忽略目录。初始化的五份个人凭证在本机私有 JSON 文件中；每个成员使用自己的 `credential` 值登录，可在不同浏览器中恢复自己的任务。不要将凭证、数据库或整个私有包上传公开仓库或粘贴到聊天中。

服务器打印 `COURT_CAMPUS_WEB_READY` 后，本机自动检查须另行得到 `CAMPUS_PEER_HTTP_OK`，才能确认来自该电脑的异机通路。等待超时只保存私有 `campus-peer-check.local.json`，不重传包、不改防火墙；它不证明学校所有子网都可达，也不证明新版本已替换已有活跃服务。

## 安装器连续执行的内容

- 核对 Linux Python 3.10、当前接口上的显式校园 IPv4、端口空闲、磁盘、GPU UUID 与空闲门禁；不会占用其他端口、停止其他模型或修改防火墙。
- 校验原 ASR 报告、venv 归属和权重 SHA256。创建独立 Web venv，以只读 Python 路径复用原 ASR 包；原 ASR 环境不重装、不删除。离线安装新 Web wheel 并运行 `pip check`。
- 提取固定发布的 FFmpeg/ffprobe，保留构建包及来源摘要。应用和运行结果分别存放，服务只写任务、上传临时文件和报告目录。
- 以现有已授权账号运行专属 systemd 服务，用 `ProtectHome=tmpfs` 和选择性 bind 限制可见目录；不新建 Linux 账号、不调整用户组。它缩小文件访问范围，不等同于使用独立 Unix 身份进行租户隔离。
- 先在回环地址、正式服务沙箱内，用公开英文控制录音生成视频，完成登录、真实上传、GPU 子进程转写、Markdown 导出和跨用户拒绝访问。任务完成标志仅在识别子进程退出后发布，并检查结束后的 GPU 占用。
- 仅在上述流程通过后启用指定校园 IP/端口与开机常驻，打印 `COURT_CAMPUS_WEB_READY`、URL 和阶段摘要。仍需从校园网另一台设备实际访问，不能据服务器自己打开页面就宣称全校园可达。

该入口为显式 HTTP 测试模式，网络传输未加密，仅使用获授权的测试素材。Cookie 不按端口隔离；同 IP 上其他站点不构成独立安全域。面向真实敏感材料的后续部署需要独立主机名、HTTPS 和进一步运行隔离，不把测试版宣称为生产合规系统。

## 使用及管理

用户登录后仅查看自己的任务。任务列表、编辑、取消、重试、音视频、抽帧和全部导出都校验归属。退出不取消已提交任务；会话空闲 12 小时或总计 7 天到期，重新登录可继续查看。失效及切换账号会重载页面，清除前一账号的页面状态。

接口约定：`GET /api/auth/session` 返回是否要求登录及当前会话状态；`POST /api/auth/login` 接收 `{"credential":"<个人随机凭证>"}`，校验精确 Origin 后签发 HttpOnly Cookie；写入请求在 `X-CSRF-Token` 中提交会话接口返回的 token。`POST /api/auth/logout` 撤销当前会话。所有 `/api/tasks` 及其子路径在校园模式均需会话，指定任务还需归属匹配；无权限任务统一返回 404，未登录为 401，来源/CSRF 不符为 403，超限为 413/429，磁盘预留不足为 507。Cookie、凭证及 CSRF 不放 URL 或浏览器持久存储。

管理员可在服务器应用发布目录，用 Web venv 的 Python 执行 `-m court_transcript.access --database <私有数据库> --add-user <显示名称> --output <新私有文件>`，或 `--revoke-user <用户ID>`。凭证只写指定新文件，不打印；撤销立即使对应会话失效，不自动取消任务。命令通过既有服务器管理员权限运行，不创建 Linux 账号。

上传最多两个并行入口，处理队列最多八个、识别串行执行。校园模式为 multipart 暂存和落盘副本共同预留空间，保留磁盘余量。逐任务子进程正常完成、失败或取消后退出；指定 GPU 是运行选择，不是显存硬配额。说话人分离、重叠分离及角色 Agent 仍按实际依赖和权重启用，未安装时不会伪造结果。

## 失败与恢复

上传失败重跑本机入口可校验续传。安装失败保留归档、独立 venv、任务和诊断；只停止描述标记匹配的自有服务，不处理 GLM 等其他服务。重新运行同一安装块可复用完整且归属匹配的发布文件，未知或校验不一致的目录会拒绝覆盖。服务已活跃时安装器会拒绝直接重装，避免中断成员任务。

私有 `status.json` 记录阶段，`reports/service-diagnostics.log` 记录本服务的诊断，任务目录包含 `worker.log`。不要公开整个日志或数据库；回传输出前保留凭证文件在本机。中文庭审质量、说话人归属和重叠恢复不由工程控制录音验收。

## 当前安全资料与依赖基线

2026-10-29 核对采用服务端 opaque 会话、随机个人访问凭证、CSRF 和逐对象授权；未实现实名认证、MFA 或 NIST AAL 等级声明。依据：[NIST SP 800-63B-4](https://csrc.nist.gov/pubs/sp/800/63/b/4/final)、[OWASP 会话](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)、[CSRF](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)、[对象授权](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)。

校园包锁定 FastAPI 0.162.0、Starlette 1.7.0（兼容 Python 3.10）、python-multipart 0.0.32、Pydantic 2.14.1、Uvicorn 0.54.0，并校验 PyPI 发布摘要。该选择覆盖本轮核对的 [Host/路径鉴权公告](https://github.com/Kludex/starlette/security/advisories/GHSA-86qp-5c8j-p5mr)、[表单限额公告](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq)及 [multipart 解析公告](https://github.com/Kludex/python-multipart/security/advisories/GHSA-5rvq-cxj2-64vf)，不代表今后没有新漏洞。鉴权使用原始 ASGI path，Host 白名单在外层执行。服务目录配置参考 [systemd.exec](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)。
