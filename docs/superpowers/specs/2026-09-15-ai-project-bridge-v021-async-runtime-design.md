# AI Project Bridge V0.2.1 Async Runtime Design

**Status:** Approved for implementation on `feat/v02-research-bridge`

**Goal:** Make code-task startup asynchronous while preserving the existing Bridge security model and providing a durable task/event runtime that can be resumed or safely marked unknown after restart.

## Scope

This iteration covers only asynchronous code tasks, durable task metadata and snapshots, persisted event streams, the shared worker runtime, and Fake/Real Codex app-server smoke tests. Experiment review, presentation, WPS, GitHub transport, and realtime dashboards remain on their existing V0.2 paths.

## Architecture

The runtime call chain is:

```text
MCP / GitHub transport
        |
        v
TaskSupervisor
        |
        v
WorkerQueue (ThreadPoolExecutor)
        |
        v
TaskRunner (task-type routing)
        |
        v
CodeExecutor
        |
        v
CodexSessionManager
        |
        v
Codex app-server
```

Transport handlers only validate and call `TaskSupervisor`. `TaskSupervisor` creates the durable `QUEUED` task before submitting work. `WorkerQueue` owns futures and worker execution mechanics; `TaskRunner` owns task-type dispatch and the code-task lifecycle. No MCP tool or transport creates a thread or directly invokes Codex, Git, worktrees, or commands.

The existing worktree manager, allowed-command policy, workspace-write behavior, `shell=False`, artifact restrictions, trusted actor checks, and GitHub transport remain unchanged. The new runtime calls those existing boundaries through the current supervisor/executor path.

## Durable task model

`bridge/store/models.py` defines the public task snapshot and event-shaped data structures. `bridge/store/task_store.py` provides a filesystem-backed `TaskStore` with these operations:

- `create_task()` creates a task directory and persists `task.json` in `QUEUED` state before work is submitted.
- `get_task()` returns the current snapshot.
- `update_task()` updates non-transition metadata such as thread/turn IDs, stage, changed files, and event cursor.
- `transition_task()` validates an allowed state transition and persists the resulting snapshot through the EventBus path.
- `append_event()` and `list_events()` provide monotonic JSONL event storage and `after_seq`/`limit` pagination.
- `save_artifacts()` stores a bounded manifest only; it never embeds large file contents.

`task.json` contains the required public fields: `task_id`, `project`, `task_type`, `state`, `created_at`, `updated_at`, `thread_id`, `turn_id`, `worktree`, `events_file`, and `artifact_manifest`. Existing V0.2 `task.yaml`, `state.json`, `events.jsonl`, logs, and `result.json` files remain readable for compatibility; new writes keep the compatibility projection where existing code depends on it.

All JSON snapshots use the existing atomic-write pattern extended to flush and `fsync` the temporary file before `os.replace`. No production path directly opens `task.json` with write mode.

## Event model

`TaskEventBus` is the single event-writing boundary for the new runtime. Every event has this shape:

```json
{
  "seq": 1,
  "task_id": "task-...",
  "type": "state_changed",
  "time": "2026-09-15T00:00:00+00:00",
  "data": {}
}
```

The event sequence is monotonic per task and is persisted with the task snapshot. Required event types include `task_created`, `state_changed`, `thread_started`, `turn_started`, `turn_completed`, `file_changed`, `command_started`, `command_completed`, `artifact_created`, and `error`. Agent messages, bounded command-result summaries, and file-change summaries may be retained. Chain of thought and raw reasoning are removed before persistence.

State changes are validated against the state machine and written as `state_changed` events. The supported runtime transitions are:

```text
QUEUED -> PREPARING
PREPARING -> RUNNING
RUNNING -> WAITING_REVIEW | FAILED
WAITING_REVIEW -> RUNNING | COMPLETED
```

`UNKNOWN` represents a task whose live state cannot be verified after restart. Recovery may move `UNKNOWN -> RUNNING` only after a safe Codex resume is established, or `UNKNOWN -> CANCELLED` when explicit cancellation support is available. The current code-task control surface continues to expose only the approved `steer`, `interrupt`, `continue`, and `accept` actions.

## Asynchronous startup and execution

`TaskSupervisor.start_code_task()` performs project/capability/instruction validation, creates the task, emits `task_created` and the initial `state_changed`, submits a `TaskRunner` job, and immediately returns exactly:

```json
{
  "task_id": "task-...",
  "state": "QUEUED",
  "project": "registered-project"
}
```

It does not wait for worktree creation, Codex startup, or turn completion. The worker transitions the task to `PREPARING`, prepares the existing isolated worktree, starts/resumes the Codex session, persists `thread_id` and `turn_id`, and waits for the session result. A completed turn becomes `WAITING_REVIEW`; an exception becomes `FAILED` with a safe error event. Existing status, control, and artifact methods continue to return bounded public data.

`TaskRunner` has a code handler now and explicit unsupported-task handling for future `experiment-review` and `presentation` routing. Those task types are not migrated in this iteration.

## Restart recovery

On Bridge startup, `TaskSupervisor` asks the shared runtime to recover persisted tasks. `QUEUED` tasks are re-submitted using their stored task metadata. `PREPARING` and `RUNNING` tasks are inspected for a valid registered project, trusted worktree under the Bridge worktree root, and a persisted thread ID. If a safe resume can be established, the runner resumes the task; otherwise the task is marked `UNKNOWN` and the reason is persisted as an event/snapshot update. Recovery never leaves an unverifiable task falsely reported as `RUNNING`.

## MCP contract

The existing MCP server remains newline-delimited JSON-RPC with no HTTP listener. All six required task tools call the supervisor:

- `bridge_list_projects` returns only project IDs and public capabilities.
- `bridge_start_code_task` returns immediately with `QUEUED`.
- `bridge_task_status` reads the current bounded snapshot.
- `bridge_task_events` returns only events after `after_seq`, bounded by `limit`.
- `bridge_control_task` delegates state-checked control operations.
- `bridge_task_artifacts` returns only a bounded manifest (`path`, `size`/`bytes`, `kind`, and optional hash).

Schema validation continues to reject unknown fields, unsafe identifiers, absolute paths, and arbitrary instructions that bypass the existing task boundaries.

## Verification

Unit tests cover TaskStore creation/update/transition/event/reload behavior, EventBus sequence and persistence, WorkerQueue futures and exception capture, TaskRunner success/failure lifecycle, and immediate supervisor startup. Smoke tests use the existing injectable FakeAppServer to verify initialize, model/list, thread/start, turn/start, notification completion, persisted IDs, state changes, and events. `scripts/codex_smoke_test.py` is manual-only: it creates a temporary directory, discovers `codex`, runs `codex app-server --stdio` for the simple hello task, reports IDs/events/changed files, and never modifies or commits/pushes the real project or reads secrets.

