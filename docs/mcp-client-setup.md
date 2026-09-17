# Compatible Local MCP Client 的 MCP 配置

本项目的 MCP server 使用标准 STDIO transport，不启动 HTTP 或 WebSocket 服务。Compatible local MCP clients may launch the STDIO server directly，通过该进程的 `stdin/stdout` 调用 Bridge 工具。ChatGPT Web 不直接启动本地 Python；它通过 OpenAI Secure MCP Tunnel 到达本地 STDIO MCP server。完整 Web 接入步骤见 [`docs/chatgpt-web-tunnel.md`](chatgpt-web-tunnel.md)。

## 1. 安装并验证入口

在 Bridge 仓库根目录，用实际使用的 Python 安装项目：

```powershell
$Python = 'C:\Users\29833\.conda\envs\py10\python.exe'
& $Python -m pip install -e '.[test]'
& $Python -m bridge.mcp.server --help
```

准备被 `.gitignore` 忽略的 `config.local.yaml`。其中只能登记本机信任的项目根目录、Git 仓库和允许执行的命令；不要把 token、密码或其他认证信息写入配置。

## 2. Compatible local MCP client 配置

如果 `python` 已经能找到已安装的 Bridge，compatible local MCP client 的最小配置如下：

```json
{
  "mcpServers": {
    "ai-project-bridge": {
      "command": "python",
      "args": [
        "-m",
        "bridge.mcp.server"
      ]
    }
  }
}
```

入口默认读取进程工作目录下的 `config.local.yaml`。在 Windows 本地 MCP client 场景中，推荐把 Python、模块和配置路径都固定为实际绝对路径：

```json
{
  "mcpServers": {
    "ai-project-bridge": {
      "command": "C:\\Users\\29833\\.conda\\envs\\py10\\python.exe",
      "args": [
        "-m",
        "bridge.mcp.server",
        "--config",
        "F:\\ai-project-bridge\\config.local.yaml"
      ]
    }
  }
}
```

请把示例中的 `F:\\ai-project-bridge` 替换成实际路径。`--config` 是本地启动参数，不是 MCP 工具参数；MCP 对话本身不能借此切换配置或项目根目录。

保存本地 client 配置后，重启或重新加载 MCP 连接。连接成功后，先调用 `bridge_list_projects` 验证已注册的 workspace。ChatGPT Web 请按 [`docs/chatgpt-web-tunnel.md`](chatgpt-web-tunnel.md) 使用 Tunnel connector。

## 3. Workspace 注册与调用规则

项目 workspace 只在本机 `config.local.yaml` 注册，例如：

```yaml
projects:
  UNetMamba:
    capabilities: [code]
    root: 'D:/Projects/UNetMamba'
    repo: 'YOUR_NAME/UNetMamba'
    allowed_commands:
      quick_test:
        argv: ['python', '-m', 'pytest', '-q']
```

对话中只使用 `bridge_list_projects` 返回的项目 `id`：

```json
{
  "project": "UNetMamba",
  "instruction": "修复登录模块并运行已有测试",
  "acceptance": ["测试通过"]
}
```

ChatGPT 不能指定 `C:\\xxx`、`D:/xxx`、`cwd`、`root` 或任意仓库路径。工具只接受注册项目的 `project` ID；未知项目、绝对路径、路径穿越和未知字段都会被拒绝。实验 review 只能引用配置中已注册的 `command_id`，不能提交任意命令字符串。

典型对话调用顺序是：

1. `bridge_list_projects`
2. `bridge_start_code_task`
3. 使用返回的 `task_id` 调用 `bridge_task_status` 和 `bridge_task_events`
4. 需要干预时调用 `bridge_control_task`
5. 任务进入 review 后，调用 `bridge_task_artifacts` 查看有界 artifact manifest，再用 `bridge_control_task` 的 `accept` 完成任务

`bridge_task_artifacts` 只返回安全的 manifest 字段，不提供任意文件读取能力。工具调用始终进入 `TaskSupervisor`；MCP 层不直接执行 shell、不直接调用 Codex，也不直接操作文件。

## 4. 故障排查

- 本地 MCP client 无法启动进程：确认 `command` 是实际 Python 可执行文件，并确认执行过 `pip install -e .`。
- 配置错误：直接运行 `& $Python -m bridge.mcp.server --config F:\\ai-project-bridge\\config.local.yaml`，查看 `stderr` 中的错误。
- 没有项目可选：检查 `projects`、项目 `capabilities` 和 `config.local.yaml` 的实际路径；不要在对话中传入本机路径替代项目 ID。
- 任务执行失败：先查询 `bridge_task_status` 和 `bridge_task_events`；确认 Codex CLI 已安装、项目配置的 `root` 可用，且任务 worktree 仍由 Bridge 管理。
