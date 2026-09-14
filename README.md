# AI Project Bridge V0.1

AI Project Bridge 是一个运行在 Windows 11 本机的前台 Python 程序。它把 GitHub 私有仓库 Issue 作为任务总线，把需要推理和编辑的工作交给本地 Codex CLI，把测试、Git 检查、实验评估和 artifact 收集交给确定性脚本。

Bridge 不执行远程 Issue 中的任意 shell、PowerShell、Python、绝对路径或环境变量。真实项目根目录和允许执行的 argv 只能来自本机、被 `.gitignore` 忽略的 `config.local.yaml`。

## 1. 安装

要求：

- Windows 11
- Python 3.9+
- Git
- GitHub CLI `gh`
- 已登录的 Codex CLI 或 Codex Desktop（Desktop bundled `codex.exe` 会自动探测，无需手工加入 PATH）
- 可选：Microsoft PowerPoint 或 LibreOffice，用于 PPT 渲染

在仓库根目录执行：

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m pip install -e '.[test]'
# Optional PPT rendering and PDF contact sheets:
& $Python -m pip install -e '.[presentation]'
Copy-Item config.local.yaml.example config.local.yaml
```

如果 `gh` 刚安装、当前 PowerShell 尚未刷新 PATH，可重新打开 PowerShell；Bridge 也会探测默认安装位置 `C:\Program Files\GitHub CLI\gh.exe`。

## 2. GitHub 认证

Bridge 使用已有的 GitHub CLI 登录态，不要求创建或填写 Personal Access Token：

```powershell
gh auth login
gh auth status
codex --version
```

Bridge 不读取 token 文件、Codex auth 文件、SSH key、浏览器数据或环境变量 secret。

## 3. 填写 config.local.yaml

至少填写：

```yaml
control_repo: "YOUR_NAME/ai-project-bridge"
trusted_github_logins:
  - "YOUR-NAME"
poll_seconds: 30

limits:
  max_artifact_file_mb: 25
  max_bundle_mb: 100
  max_images: 20

projects:
  unetmamba:
    capabilities: [code, experiment-review]
    root: "D:/Projects/UNetMamba"
    repo: "YOUR_NAME/UNetMamba"
    remote: origin
    base_branch: main
    allowed_commands:
      quick_test:
        argv: ["python", "-m", "pytest", "-q"]
    artifact_dirs: ["outputs", "results", "runs"]
```

`root` 是本机项目绝对路径，`repo` 是项目 GitHub repo。Issue 只能写 `project: unetmamba`，不能写 `root`、`cwd`、`repo`、命令或绝对路径。`allowed_commands` 的每个命令必须是预先注册的 argv 数组；Bridge 使用 `shell=False`。

`python_executable` 用于指定 Bridge 执行本地 Python 命令的解释器。本机配置已设为 `E:\anaconda\envs\py39\python.exe`；即使 `allowed_commands.argv` 写的是 `python`，Bridge 也会替换为该解释器。

YAML 中的 Windows 反斜杠路径必须使用单引号，例如 `root: 'E:\AI project bridge'`；也可以使用正斜杠，例如 `root: "E:/AI project bridge"`。不要把未转义的反斜杠放在 YAML 双引号中。

项目通过 `capabilities` 声明允许的 task type：`code`、`presentation`、`experiment-review`；例如科研项目可同时声明 `code` 和 `experiment-review`。旧版单一 `kind` 仍会自动迁移为单元素 capabilities，便于渐进升级。

`control_repo` 只承载任务 Issue、状态标签和评论；项目 `repo` 只来自本地配置，负责 `ai/issue-N` 分支和 Draft PR。项目 PR 只会写 `Control task: OWNER/ai-project-bridge#N`，不会使用 `Closes #N` 关闭项目仓库的同号 Issue。

## 4. 检查并初始化 GitHub labels

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge --config config.local.yaml doctor
& $Python -m bridge --config config.local.yaml setup-github
```

`setup-github` 只创建不存在的标签，不删除已有标签：

`ai-task`、`task:code`、`task:presentation`、`task:experiment-review`、`status:ready`、`status:running`、`status:review`、`status:failed`、`status:approved`。

## 5. 启动 Bridge

只扫描一次：

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge --config config.local.yaml run-once
```

持续轮询：

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge --config config.local.yaml run
```

按 `Ctrl+C` 停止前台进程。V0.1 不安装 Windows Service。

## 6. 创建 code task

在 Issue body 放入 `examples/code-task.md` 的格式，并添加标签：

- `ai-task`
- `task:code`
- `status:ready`

Bridge 会创建 `.bridge/worktrees/<project>-issue-<number>` 和 `ai/issue-<number>` branch，在 worktree 内调用当前 Codex CLI 的非交互形式：

```text
codex exec --json --sandbox workspace-write --approve-for-me --cd <worktree> -
```

随后执行本地 `quick_test`（如果已配置），收集 Git status/diff，commit，push issue branch，并创建 Draft PR。不会 push `main`/`master`，不会自动 merge。

## 7. 创建 presentation task

登记 `capabilities: [presentation]` 项目，在 Issue 中使用 `examples/presentation-task.md`。`brief`、`slides_spec`、`assets_dir`、`template` 都必须是项目 root 下的相对路径。

Codex 在隔离 worktree 中生成/修改 PPTX。Bridge 会在提交前渲染 PPTX，并把有界的 `final.pdf`、`contact_sheet.png` 和 `slides_png/slide_*.png` 写入同一 `ai/issue-N` 分支的 `review_bundle/presentation/`，因此原 Draft PR 可直接在 ChatGPT Web 审查。Windows 上优先通过 PowerPoint COM 检测 Office，不要求 `POWERPNT.EXE` 在 PATH；不可用时尝试 LibreOffice。每次 doctor 会分别报告 PowerPoint COM、LibreOffice 和 PDF→PNG 能力。请用 `.[presentation]` 安装可选渲染依赖。

## 8. 创建 experiment-review task

使用 `examples/experiment-review-task.md` 并添加：

- `ai-task`
- `task:experiment-review`
- `status:ready`

Issue 只能指定 config 中存在的 `command_id`，例如 `evaluate`。Bridge 不调用 Codex，而是在项目 root 使用已注册 argv，收集 `json/csv/txt/log/png/jpg/jpeg/webp`，限制单文件、总 bundle 和图片数量。experiment-review 可选 `source_issue: <positive integer>`，此时 Bridge 只接受本地同项目 code task 已登记且仍存在的候选 worktree，Issue 不能直接提供 cwd、branch 或路径。`.pth`、`.pt`、`.ckpt`、dataset 和 checkpoint 路径默认拒绝。V0.1 已预留 `SampleSelector` 接口，但不做复杂的 worst/regression 算法。

## 9. Web ChatGPT 审计与 review bundle

每个任务的本地状态在：

```text
.bridge/tasks/<issue-number>/
  task.yaml
  state.json
  events.jsonl
  runs/
    001-initial.events.jsonl
    002-rework.events.jsonl
  stdout.log
  stderr.log
  result.json
  review_bundle/
    manifest.json
    summary.md
```

代码任务另外包含 `diff.patch` 和 `diff-stat.txt`；每轮 Codex 的 JSONL 事件写入 `runs/` 且 state/result 保留 run 记录；实验任务包含 bounded metrics/artifacts；PPT 任务在项目分支的 `review_bundle/presentation/` 包含 `final.pptx` 及可用的 PDF/PNG 审查文件。

ChatGPT Web 可以审计 Draft PR 的 diff、测试结果和 Issue 评论中的 review bundle 摘要。不要把大数据、checkpoint 或 secrets 上传到 GitHub；超限文件只在 Issue 中报告未上传原因和本地路径。

## 10. 提交 rework comment

在原 Issue 中追加 `examples/rework-comment.md` 格式：

```markdown
<!-- AI_BRIDGE_REWORK -->

```yaml
instruction: |
  Fix the reviewed issue and add a regression test.
```
```

Bridge 只接受 `trusted_github_logins` 中 GitHub 用户提交的任务 Issue 和 rework comment；`trusted_github_logins` 为空时配置校验失败。未受信任的 comment 会被忽略，不调用 Codex，也不会把任务标为失败。可信 rework 会使用 `state.json` 中保存的 Codex thread/session id，并显式复用 workspace-write、非交互审批策略、当前 worktree cwd；返回 thread id 不一致时以 `CodexResumeMismatchError` 失败，不覆盖原 thread。

## 11. 出错恢复

查看任务：

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge --config config.local.yaml show-task 123
```

修复本地配置、项目 Git 状态或依赖后重试：

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge --config config.local.yaml retry 123
& $Python -m bridge --config config.local.yaml run-once
```

`status:failed` 任务不会在每次轮询中自动重复；必须显式 `retry`。Bridge 不会删除用户项目根目录，也不会强制覆盖项目的 `AGENTS.md`。

## 12. 测试与验证

```powershell
$Python = 'E:\anaconda\envs\py39\python.exe'
& $Python -m bridge doctor
& $Python -m pytest
```

自动测试使用 fake runner/fake GitHub 和临时 demo Git repository，不消耗真实 Codex 用量。真实 Codex smoke test 应由用户在确认模型用量和目标 worktree 后自行决定；不要把真实科研项目作为第一轮测试目标。

## 13. 安全边界

- 只处理同时有 `ai-task` 与合法状态的 Issue，并要求恰好一个 task type label。
- 严格 schema 校验，未知 `task_type`、未知字段、任意命令和非法路径都会拒绝。
- 远程路径必须 resolve 到本地注册项目 root 内。
- Codex 只在任务 worktree 且 `workspace-write` 权限下运行。
- 本地命令只来自 config 的 argv 白名单，禁止 shell 拼接。
- 禁止 push `main`/`master`、force push、自动 merge。
- 所有任务状态、事件流、标准输出、错误输出和结果都落盘，便于审计和恢复。

