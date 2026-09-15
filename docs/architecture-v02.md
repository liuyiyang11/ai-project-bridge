# AI Project Bridge V0.2.1 architecture

V0.2.1 hardens the durable asynchronous runtime for `code` tasks only. It retains the V0.1/V0.2 worktree, workspace-write, security validation, deterministic-command, and transport boundaries, without adding a new product runtime.

## Code-task runtime boundary

The code-task call chain is fixed:

```text
MCP / Transport
      ↓
TaskSupervisor
      ↓
WorkerQueue
      ↓
TaskRunner
      ↓
TaskHandler
      ↓
Executor
      ↓
CodexSessionManager
```

Transport code validates input and calls the Supervisor; it does not create a worktree, start Codex, or infer state from a worker Future. For MCP code startup, the entry point is `bridge_start_code_task → TaskSupervisor.start_code_task()`. The generic `start_task()` router may remain for internal compatibility, but MCP does not select the code path by inspecting `task_type`.

`TaskSupervisor.start_code_task()` creates the durable `QUEUED` task snapshot, submits a job to WorkerQueue, and immediately returns:

```json
{
  "task_id": "task-...",
  "state": "QUEUED",
  "project": "registered-project"
}
```

## Lifecycle and result contract

TaskRunner owns the runtime lifecycle and exception conversion:

```text
QUEUED → PREPARING → RUNNING → WAITING_REVIEW
                         └──────────────→ FAILED
```

TaskHandler always returns `TaskResult`:

```text
success: bool
review_ready: bool
message: str
artifacts: list
metadata: dict
```

When `success=True` and `review_ready=True`, TaskRunner transitions `RUNNING → WAITING_REVIEW`. When `success=False`, it transitions to `FAILED`. TaskRunner does not read the current snapshot after `handler.execute()` to guess the execution outcome.

TaskHandler adapts task-type-specific work. Executor owns worktree preparation, Codex interaction, and file modification. CodexSessionManager owns the app-server session protocol and durable thread/turn identity; it does not own WorkerQueue scheduling.

## Durable state and events

The TaskStore snapshot is the single business-state source for status consumers. It reads and writes durable files, stores non-state metadata, and stores bounded artifact manifests. A Future being done, cancelled, or exceptional is not a business task state.

`TaskEventBus` (called EventBus here) is the only runtime boundary allowed to change business state. It validates the transition, appends the safe event, assigns the monotonically increasing per-task `event_seq`, and updates the snapshot state/cursor together. Direct business calls such as `TaskStore.update_task(state=...)` are prohibited; TaskStore metadata writes do not replace EventBus transitions.

## Worker scheduling and recovery

WorkerQueue owns only Future submission, cancellation, and shutdown. It does not expose or define `QUEUED`, `RUNNING`, `FAILED`, or `COMPLETED` business statuses.

On restart, persisted `QUEUED` code tasks are re-enqueued. For `PREPARING` or `RUNNING`, recovery first checks whether the registered project, trusted worktree, and persisted session can be safely resumed. If it cannot establish that condition, it records `UNKNOWN`; it does not convert an unverifiable in-flight task directly to `FAILED`.

## Compatibility and exclusions

`experiment-review` and `presentation` remain compatibility interfaces and retain their existing paths. They are not migrated to the V0.2.1 code-task async runtime or its WorkerQueue/TaskRunner lifecycle contract.

WPS, Dashboard, and Web API are explicitly excluded from V0.2.1. `McpStdioServer` remains a newline-delimited JSON-RPC adapter with no HTTP listener. Its registered tools continue to reject arbitrary cwd, absolute paths, shells, and Python expressions; see [mcp-tools.md](mcp-tools.md).
