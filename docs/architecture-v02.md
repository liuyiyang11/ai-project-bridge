# AI Project Bridge V0.2 architecture

V0.2 keeps the V0.1 worktree, workspace-write, deterministic test, experiment-review, presentation-review, and optional Draft PR workflow.  The execution core is now transport-independent:

```text
GitHub Issue / MCP stdio
          |
Unified TaskSupervisor
          |
CodexSessionManager + TaskEventBus + TaskStore
          |
Code / Experiment Review / Presentation paths
```

## Phase 1

`CodexAppServerClient` starts the bundled native Codex executable with:

```text
codex app-server --stdio
```

It performs the JSON-RPC `initialize`/`initialized` handshake, then supports `model/list`, `thread/start`, `thread/resume`, `turn/start`, `turn/steer`, and `turn/interrupt`.  Notifications are read continuously from stdout and stderr is captured separately.  Raw reasoning notifications are discarded before persistence.

`CodexModelCatalog` validates model and reasoning-effort combinations from the runtime catalog.  `inherit` allows Codex defaults; `explicit` requires both values before `turn/start`.

`CodexSessionManager` persists thread/turn IDs and uses `thread/resume` after a Bridge restart.  Its public states are `QUEUED`, `PREPARING`, `RUNNING`, `WAITING_REVIEW`, `COMPLETED`, `INTERRUPTED`, and `FAILED`.

## Phase 2

`TaskSupervisor` owns task lifecycle and does not import or require GitHub.  `TaskEventBus` assigns monotonic sequence numbers and persists safe structured events.  GitHub's existing `Dispatcher` remains a compatibility transport and injects the session manager into code/presentation executors.

## Phase 3

`McpStdioServer` is a newline-delimited JSON-RPC MCP adapter.  It creates no HTTP listener and exposes only the registered Bridge tools documented in [mcp-tools.md](mcp-tools.md).  Tool handlers never accept cwd, absolute paths, arbitrary shell, or arbitrary Python expressions.
