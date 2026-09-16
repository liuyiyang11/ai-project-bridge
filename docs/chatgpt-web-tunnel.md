# ChatGPT Web 通过 Secure MCP Tunnel 接入 Bridge

本页记录 AI Project Bridge 的两条 MCP 接入路径。Part 1 只实现仓库内的 profile、doctor/start wrapper 和离线测试；账号级 tunnel、OpenAI 登录和 ChatGPT Web 操作属于 Part 2，本页中的相关步骤目前均未执行。

## 两条路径

```text
Compatible local MCP client
    -> STDIO
    -> ai-project-bridge MCP

ChatGPT Web
    -> OpenAI Secure MCP Tunnel
    -> official tunnel-client
    -> local MCP STDIO child
    -> ai-project-bridge MCP
```

Bridge 本体仍是本地 newline-delimited JSON-RPC/STDIO server：

```powershell
python -m bridge.mcp.server --config F:\ai-project-bridge\config.local.yaml
```

Bridge 不监听公网 HTTP 端口，不实现 WebSocket、ngrok、Cloudflare Tunnel 或自制认证。ChatGPT Web 不直接启动本机 Python；它通过 OpenAI Secure MCP Tunnel 把请求交给本机 `tunnel-client`，再由 `tunnel-client` 启动上面的 STDIO child。

## 官方 tunnel-client

只从 OpenAI 官方来源获取 Windows 客户端：

- [OpenAI Platform Tunnels 管理页](https://platform.openai.com/settings/organization/tunnels)中的下载入口；
- [openai/tunnel-client Releases](https://github.com/openai/tunnel-client/releases)；
- [官方 README](https://github.com/openai/tunnel-client/blob/master/README.md)、[onboarding](https://github.com/openai/tunnel-client/blob/master/docs/onboarding.md)、[configuration](https://github.com/openai/tunnel-client/blob/master/docs/configuration.md) 和 [troubleshooting](https://github.com/openai/tunnel-client/blob/master/docs/troubleshooting.md)。

Windows 安装完成后，在新的 PowerShell 中检查：

```powershell
tunnel-client --version
tunnel-client help quickstart
```

本仓库不从第三方下载客户端，也不在 Python 包中重新实现 Tunnel 协议。

## Profile 设计

仓库 wrapper 默认使用被 `.gitignore` 忽略的本地文件：

```text
E:\AI project bridge\.bridge\tunnel-profile.yaml
```

生成 profile：

```powershell
$Python = 'C:\Users\29833\.conda\envs\py10\python.exe'
$Config = (Resolve-Path '.\config.local.yaml').Path
.\scripts\tunnel_profile.ps1 -PythonExecutable $Python -ConfigPath $Config
```

如果配置路径不在当前目录，也可以显式传入实际绝对路径：

```powershell
.\scripts\tunnel_profile.ps1 `
  -PythonExecutable 'C:\Users\29833\.conda\envs\py10\python.exe' `
  -ConfigPath 'F:\ai-project-bridge\config.local.yaml'
```

profile 遵循官方 YAML schema version 1，核心部分等价于：

```yaml
config_version: 1
control_plane:
  base_url: https://api.openai.com
  api_key: env:CONTROL_PLANE_API_KEY
health:
  listen_addr: 127.0.0.1:8080
  url_file: local .bridge path/tunnel-health.url
admin_ui:
  open_browser: false
mcp:
  commands:
    - channel: main
      command: '"python.exe" -m bridge.mcp.server --config "config.local.yaml"'
```

实际生成的 profile 使用绝对路径并且不写入 `tunnel_id`；运行时从 `CONTROL_PLANE_TUNNEL_ID` 读取。`mcp.commands` 的 `main` binding 直接启动本地 STDIO MCP，不生成 `mcp.server_urls`，因此 Bridge 不需要监听公网端口。

## Secret 处理

`CONTROL_PLANE_API_KEY` 是 daemon 的 runtime key，用于 `doctor` 和 `run`。`OPENAI_ADMIN_KEY` 只用于 `tunnel-client admin tunnels list/create/get/update/delete` 等 tunnel 管理操作，不能用于长期运行的 daemon。

profile 只保存：

```yaml
api_key: env:CONTROL_PLANE_API_KEY
```

不会保存 key 的 literal 值、admin key、tunnel token 或其它 credential，也不会把 key 作为命令行参数传给 `tunnel-client`。PowerShell 环境变量只应在当前会话中设置，完成后清理：

```powershell
$env:CONTROL_PLANE_TUNNEL_ID = 'tunnel_0123456789abcdef0123456789abcdef'
$secureKey = Read-Host 'Runtime API key' -AsSecureString
$env:CONTROL_PLANE_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
Remove-Variable secureKey

# 完成 doctor/run 后清理当前 PowerShell 会话：
Remove-Item Env:CONTROL_PLANE_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:CONTROL_PLANE_TUNNEL_ID -ErrorAction SilentlyContinue
```

上面的 tunnel ID 只是格式示例，不是可用凭据。不要把真实 key 粘贴进脚本、profile、Issue、日志或 Git。

## Doctor 与启动

先生成 profile，再运行：

```powershell
.\scripts\tunnel_doctor.ps1 `
  -PythonExecutable $Python `
  -ConfigPath $Config
```

wrapper 在远程 doctor 前 fail closed 检查 `tunnel-client`、profile、本地 Python、Bridge config、runtime key 和 tunnel ID。失败会标明具体层：

```text
CONTROL_PLANE
TUNNEL_AUTH
LOCAL_MCP_START
MCP_INITIALIZE
TOOLS_DISCOVERY
CODEX_DISCOVERY
```

doctor 通过后，在前台启动官方客户端：

```powershell
.\scripts\tunnel_start.ps1 `
  -PythonExecutable $Python `
  -ConfigPath $Config
```

`tunnel_start.ps1` 不使用 `Start-Process`，不创建后台 supervisor；它保持官方的 foreground `tunnel-client run --profile-file ...` 行为。使用 STDIO binding 时，同一个 tunnel ID 只运行一个活跃 `tunnel-client` 实例，重启时先停止旧实例。

另开 PowerShell 检查官方本地 operator endpoints：

```powershell
Invoke-RestMethod 'http://127.0.0.1:8080/healthz'
Invoke-RestMethod 'http://127.0.0.1:8080/readyz'
Invoke-WebRequest 'http://127.0.0.1:8080/ui' -UseBasicParsing
```

`/healthz` 只说明进程存活；`/readyz` 才用于判断 MCP probe/readiness 是否完成。

## Part 2：账号与 ChatGPT Web 手工验收

以下命令和步骤是未来的准确验收顺序，本轮不执行。

### 1. 获取或复用 tunnel ID

优先在 [Platform Tunnels 管理页](https://platform.openai.com/settings/organization/tunnels)复用已有 tunnel，不要为同一个本地 Bridge 自动创建重复 tunnel。若组织允许自助管理，也可使用 admin key 执行管理命令；admin key 只存在于当前会话：

```powershell
$adminSecureKey = Read-Host 'Admin API key for tunnel CRUD only' -AsSecureString
$env:OPENAI_ADMIN_KEY = [System.Net.NetworkCredential]::new('', $adminSecureKey).Password
Remove-Variable adminSecureKey

tunnel-client admin tunnels list --workspace-id '<WORKSPACE_ID>' --json
tunnel-client admin tunnels get 'tunnel_0123456789abcdef0123456789abcdef'

# 只有明确需要新 tunnel 且具备 Manage 权限时才执行：
tunnel-client admin tunnels create --name 'ai-project-bridge' --workspace-id '<WORKSPACE_ID>'

Remove-Item Env:OPENAI_ADMIN_KEY -ErrorAction SilentlyContinue
```

`<WORKSPACE_ID>` 和返回的 tunnel ID 都必须替换为真实值；本轮不会执行这些账号级命令。runtime daemon 使用单独的 `CONTROL_PLANE_API_KEY`，不能把 `OPENAI_ADMIN_KEY` 带进 daemon 会话。

### 2. 安装、配置和 doctor

```powershell
tunnel-client --version
tunnel-client help quickstart

$env:CONTROL_PLANE_TUNNEL_ID = 'tunnel_0123456789abcdef0123456789abcdef'
$secureKey = Read-Host 'Runtime API key' -AsSecureString
$env:CONTROL_PLANE_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
Remove-Variable secureKey

.\scripts\tunnel_profile.ps1 -PythonExecutable $Python -ConfigPath $Config
.\scripts\tunnel_doctor.ps1 -PythonExecutable $Python -ConfigPath $Config
.\scripts\tunnel_start.ps1 -PythonExecutable $Python -ConfigPath $Config
```

记录时只记录 tunnel ID、profile 名/路径、health 和 readiness；不要记录 key。确认 `/healthz`、`/readyz` 和（如当前版本提供）`/ui`，然后保持 `tunnel_start.ps1` 前台进程运行。

### 3. 配置 ChatGPT Web Connector

在 ChatGPT Web 中：

```text
Settings
  -> Connectors / Apps
  -> Connection: Tunnel
  -> 选择已有 tunnel 或输入对应 tunnel_id
  -> 扫描/加载工具
```

不要在 ChatGPT Web 中填本机 MCP URL、Windows 路径、`cwd` 或 Bridge project root。期望至少发现当前九个 Bridge 工具：

1. `bridge_list_projects`
2. `bridge_codex_catalog`
3. `bridge_start_code_task`
4. `bridge_start_experiment_review`
5. `bridge_start_presentation_task`
6. `bridge_task_status`
7. `bridge_task_events`
8. `bridge_control_task`
9. `bridge_task_artifacts`

### 4. 三组真实 smoke

保持 tunnel 在线，从 ChatGPT Web 依次执行：

1. 调用 `bridge_list_projects`，确认只返回本机配置登记的 project ID 和 public capabilities。
2. 调用 `bridge_start_code_task`，使用很小的 read-only instruction，例如“读取 README 和当前版本信息，不修改任何文件，总结当前 Bridge 架构”。确认任务经过 `QUEUED`/`RUNNING` 后到达 `WAITING_REVIEW`。
3. 使用返回的 `task_id` 调用 `bridge_task_status`、`bridge_task_events` 和 `bridge_task_artifacts`，确认结果通过 Tunnel 返回；调用 `bridge_control_task(action=accept)`，确认 `WAITING_REVIEW -> COMPLETED`。
4. 另起一个 read-only 任务，在 `RUNNING` 时调用 `bridge_control_task(action=steer, instruction=...)`，确认同一个 Codex thread 继续执行。
5. 再起任务，在 `RUNNING` 时调用 `bridge_control_task(action=interrupt)`，确认 `RUNNING -> INTERRUPTED`；等待至少 10 秒，确认迟到 completion 不能覆盖 `INTERRUPTED`。

### Part 2 当前状态

本 Part 1 实现不会声称以下项目已验证：

- Windows `tunnel-client` 安装、版本和 executable 路径；
- control plane reachability、runtime authentication 和真实 tunnel metadata；
- 真实 `tunnel-client doctor`、`run`、`/healthz`、`/readyz`、`/ui`；
- ChatGPT Web Connector 配置和九个工具发现；
- ChatGPT Web `bridge_list_projects` 真实结果；
- ChatGPT Web -> Tunnel -> Bridge -> Codex 的 `WAITING_REVIEW` 任务；
- `status`、`events`、`artifacts`、`accept`、`steer`、`interrupt` 以及迟到 completion 保护。

只有这些步骤全部由人工真实执行并记录结果后，才可标记 `V0.3.6 Secure MCP Tunnel Integration Verified`。
