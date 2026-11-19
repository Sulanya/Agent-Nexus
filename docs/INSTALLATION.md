# 安装、配置与连接

## 前置条件

- Python 3.11（网关使用 tomllib 等标准库能力）。
- 已安装且可在当前环境调用的 Codex；登录/provider 配置由 Codex 自己管理。
- Claude Code、WorkBuddy、Node.js 按需安装，不随 AN 分发。
- 现代移动或桌面浏览器；公网入口建议 HTTPS。

## 源码入口

按照 README 建虚拟环境，安装 requirements.txt，复制 an.config.example.json 后运行 tools/dev.py。

| 命令 | 行为 |
|---|---|
| start | 检查端口与归属；启动本目录网关，Windows 使用 base pythonw，无重复实例 |
| status | 本地 health、PID 和实例归属检查 |
| open | 验证本地完整权限，然后直接打开本机私人入口 |
| connect --base-url URL | 验证指定入口，生成本地完整/只读连接页及可用二维码 |
| stop | 核对 health PID 和源码路径，拒绝停止有活动回合/审批/队列的实例 |

源码启动器不会自动安装开机任务、改防火墙、代理或桌面 Agent 配置。它也不自动建立公网隧道。部署为常驻服务前请完成平台验收。

## 配置

an.config.json 是本地私有配置，已被 .gitignore 排除。默认 host=127.0.0.1、port=8790、tunnel=false、allowFullAccess=false。allowFullAccess 影响模型执行权限，不应为解决连接问题而开启。请在批准执行前理解 Agent 的本机工具权限。

CODEX_HOME 可覆盖 Codex 数据目录；WORKBUDDY_CLI/WORKBUDDY_NODE/CLAUDE_BIN_DIRS 提供引擎发现覆盖。AN_ROUTER_URL 是可选用量/上下文元数据服务地址，默认不请求任何私人 router。AN_CONTEXT_LIMITS 可指定你自己的上下文测量文件；AN 不预置作者的模型/provider 名单。

## 移动连接

可信局域网需要显式监听可达地址并配置防火墙；VPN/隧道/反向代理可以让远程设备连接。Quick Tunnel 的临时域名不是永久地址，重建后需要重新生成连接页。源码包不附带 cloudflared，使用该工具时应自行安装并遵守其条款。

tools/dev.py connect 会将含凭据的入口保存到 _run/OWNER-CONNECT.html。这个目录不会进入 Git。公网裸地址返回认证错误是正常行为：认证用于保护本机文件和执行能力。不要通过全局去掉认证来修复私人链接缺失。

## 桌面控制面板与打包

launcher/app.py 和 launcher/web 保留桌面控制面板源码；安装 requirements-desktop.txt 可在 Windows 上开发验证。START/STOP/STATUS/LINK 包装命令使用源码入口；已有 tools/an.ps1、tools/an.sh 和平台包装入口一并提供，但其打包 runtime、可执行文件和平台签名工具不在源码中。源码开发优先使用 tools/dev.py；不可把既有便携发行脚本当成全平台新机已验证安装器。

语音转写为可选功能，另装 requirements-voice.txt，自行下载模型并核查许可。启动器图标可用 python launcher/make_icon.py 生成；生成图标和 demo QR 不应被误认成第三方品牌授权。

## 排错

- 401：使用私人连接页，检查域名变化和浏览器存储，不把 token 发到 Issue。
- 端口占用：换端口或处理明确的所属进程；不按 Python 进程名批量结束。
- 列表空/缺失：先核对引擎目录和登录环境；不要删除会话库来重建。
- CC/WorkBuddy 新空会话未出现在桌面：它仍可能是 AN 草稿，首条消息才触发原生会话落盘。
- 跨版本失效：提交版本、平台、匿名步骤和脱敏错误，不提交原始对话或数据库。

可选 tools/tunnel_recovery.py 保留 quick-tunnel 恢复源码；使用前自行安装 cloudflared/psutil，核对端口与进程归属。它不是默认启动的一部分。源码默认端口以外的隧道配置需要对应适配与验收。
