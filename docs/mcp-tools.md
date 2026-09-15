# MCP stdio tools

Start the local adapter with:

```powershell
& $Python -m bridge --config config.local.yaml mcp-stdio
```

The adapter is intended for MCP-compatible local clients.  A normal ChatGPT Plus chat cannot be assumed to connect to a local MCP stdio process, so GitHub transport remains the current cloud-facing entry point.

The server exposes exactly these tools. V0.2.1 makes only code-task startup asynchronous: `bridge_start_code_task` persists a `QUEUED` task and returns immediately while the shared worker runtime advances it in the background.

Its code-task call chain is:

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

| Tool | Purpose |
| --- | --- |
| `bridge_list_projects` | Registered project IDs and capabilities only; never project roots. |
| `bridge_codex_catalog` | Runtime `model/list` catalog and supported reasoning efforts. |
| `bridge_start_code_task` | The V0.2.1 async code-task entry; returns `task_id`, `state: "QUEUED"`, and `project`. |
| `bridge_start_experiment_review` | Compatibility interface for a registered deterministic command; not migrated to the V0.2.1 async code-task runtime. |
| `bridge_start_presentation_task` | Compatibility interface for presentation input paths; not migrated to the V0.2.1 async code-task runtime. |
| `bridge_task_status` | Reads the current TaskStore snapshot: state, stage, thread/turn IDs, changed files, review flag, and last event sequence. |
| `bridge_task_events` | Bounded incremental safe events written by EventBus. |
| `bridge_control_task` | `steer`, `interrupt`, `continue`, or `accept` with state checks. |
| `bridge_task_artifacts` | Bounded artifact manifest; no arbitrary file reads. |

For code startup, the MCP handler calls `TaskSupervisor.start_code_task()` directly rather than selecting a route through generic `start_task()`. WorkerQueue only owns Futures (`submit`, `cancel`, `shutdown`); it does not report business status. `bridge_task_status` therefore reads the durable snapshot rather than a Future.

EventBus is the only runtime entry for business-state changes: it appends events, maintains monotonic `event_seq`, and updates the snapshot state. TaskStore persists files, metadata, and bounded artifact manifests, but MCP handlers and other business code do not call `update_task(state=...)`.

All schemas reject unknown fields. Paths must be project-relative, experiment commands must be registered in `allowed_commands`, and `source_task_id` is resolved through `TaskStore`; a cross-project or untrusted worktree is rejected. WPS, Dashboard, and Web API are outside the V0.2.1 scope.

## Manual real Codex smoke test

The real app-server smoke test is a manual check, not a pytest result. After installing and discovering the Codex executable, run it manually from the repository root:

```powershell
python scripts/codex_smoke_test.py
```

It creates a temporary Codex workspace, runs the bounded hello-task protocol,
prints safe events and newly created files, and removes the temporary workspace
on exit. It does not open the configured project or perform Git operations.
