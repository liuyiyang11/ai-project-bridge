# AI Project Bridge V0.1 设计

## 目标

在 Windows 11 上运行一个前台 Python 进程，将带有严格标签和任务 schema 的 GitHub Issue 转换为本地、可审计的 code、presentation 或 experiment-review 执行，并将结果通过评论、Draft PR 和任务专用 review bundle 回传。远程 Issue 永远不能携带任意命令、路径或环境变量。

## 架构

系统采用分层结构：

1. `task_parser` 和 `security` 是纯本地校验核心。它们只接受固定 YAML schema，拒绝未知字段、未知 task type、绝对路径、路径穿越和命令字段。
2. `config` 从本机 `config.local.yaml` 读取项目根目录、项目 repo、允许命令和 bundle 限制。真实绝对路径只从这里取得。
3. `github` 是基于 `gh` 子进程的适配层，负责 Issue、评论、标签和 Draft PR；不读取 GitHub token，也不实现远程 shell。
4. `task_store` 在 `.bridge/tasks/<issue>` 持久化 task、state、事件、stdout、stderr 和 result，确保重复扫描不会重复执行，且 rework comment 有幂等记录。
5. `worktree`、`git_collector` 和 `artifact_collector` 负责隔离工作区、提交前审计和大小/扩展名限制。
6. `CodexRunner` 只调用当前 CLI 支持的 `codex exec - --json --sandbox workspace-write`，返工调用 `codex exec resume <thread_id> - --json`。runner 可注入 fake 实现，测试不消耗真实 Codex 用量。
7. 三个 executor 共享 dispatcher 生命周期。code 和 presentation 通过 Codex；experiment-review 只执行 config 中注册的 argv，并产生 review bundle。

## 生命周期

```text
ready -> running -> review
                 \-> failed
review + new rework comment -> running -> review
```

任务初次扫描只处理同时有 `ai-task` 和 `status:ready` 的 Issue，并要求一个合法 task label。完成后移除旧状态标签、添加新状态标签并评论简洁结果。失败保留本地日志和错误摘要。`retry` 清理失败状态但不删除历史工件。

## 安全边界

- 所有 subprocess 使用 argv 数组、`shell=False`，命令只能来自本机 config。
- Codex cwd 只能是为该任务创建的 worktree，并显式使用 `workspace-write`；不得使用 danger-full-access。
- branch 只由 Bridge 生成 `ai/issue-<number>`，禁止 main/master 和 force push。
- 远程字段不允许 cwd、绝对路径、shell、PowerShell、Python expression、环境变量或任意未知 schema 字段。
- 路径必须是项目 root 下的相对路径，并在 resolve 后再次确认位于 root 内。
- artifact collector 默认拒绝 checkpoint、dataset 和超限文件，并限制总 bundle、大文件、图片数量。
- 不读取、写入或保存 GitHub/Codex 登录凭据，不覆盖受管项目的 AGENTS.md。

## 测试策略

先用 pytest 覆盖 schema/security/state 幂等、worktree 生命周期、命令白名单、artifact 限制、Codex 缺失和 fake runner 端到端流程；再用本地 demo Git repository 验证 task parse -> worktree -> fake executor -> result -> cleanup。真实 Codex 只提供用户可选的 smoke-test 命令，不在自动验证中调用。

