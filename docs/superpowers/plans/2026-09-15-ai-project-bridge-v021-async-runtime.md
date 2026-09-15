# AI Project Bridge V0.2.1 Async Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add asynchronous code-task startup with durable snapshots, persisted incremental events, a shared worker runtime, restart recovery, and Fake/Real Codex smoke tests without changing the existing security model or migrating other task types.

**Architecture:** Keep `bridge.task_store` as a compatibility import while making `bridge/store/task_store.py` the canonical filesystem implementation. `TaskEventBus` depends on `TaskStore` and is the only runtime boundary that records state transitions and events. `TaskSupervisor` creates a queued task and submits one `TaskRunner` job to a shared `WorkerQueue`; `TaskRunner` routes to an injectable `CodeTaskHandler`, which invokes a runtime code executor and the existing `CodexSessionManager`.

**Tech Stack:** Python 3.9+, Pydantic 1.x, `concurrent.futures.ThreadPoolExecutor`, atomic JSON/JSONL filesystem persistence, pytest, existing Codex app-server stdio client and FakeAppServer.

---

### Task 1: Add canonical durable task models and store compatibility layer

**Files:**
- Create: `bridge/store/__init__.py`
- Create: `bridge/store/models.py`
- Create: `bridge/store/task_store.py`
- Modify: `bridge/task_store.py` (compatibility re-export or compatibility-preserving implementation)
- Test: `tests/unit/test_task_store.py`
- Modify: `tests/test_task_store.py` only where the new state/event contract supersedes an assertion

- [ ] **Step 1: Write the failing store tests**

Add tests that exercise the new public API and the legacy files together:

```python
def test_create_task_writes_queued_task_json_and_legacy_files(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    task = store.create_task("task-1", project="demo", task_type="code", instruction="make a change", acceptance=["tests pass"])

    assert task["task_id"] == "task-1"
    assert task["state"] == "QUEUED"
    assert json.loads((tmp_path / ".bridge" / "tasks" / "task-1" / "task.json").read_text())["events_file"] == "events.jsonl"
    assert (tmp_path / ".bridge" / "tasks" / "task-1" / "state.json").is_file()


def test_update_and_transition_task_keep_snapshot_and_legacy_projection_in_sync(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")

    store.update_task("task-1", thread_id="thread-1", stage="starting turn")
    store.transition_task("task-1", "QUEUED", "PREPARING", "worker accepted task")

    assert store.get_task("task-1")["thread_id"] == "thread-1"
    assert store.get_task("task-1")["state"] == "PREPARING"
    assert store.load_state("task-1")["state"] == "PREPARING"


def test_events_append_with_sequence_and_reload_from_new_store_instance(tmp_path):
    root = tmp_path / ".bridge"
    store = TaskStore(root)
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    store.append_event("task-1", {"seq": 1, "task_id": "task-1", "type": "task_created", "time": utc_now(), "data": {}})
    store.save_artifacts("task-1", [{"path": "hello.py", "size": 20, "kind": "source", "hash": "abc"}])

    reloaded = TaskStore(root)
    assert reloaded.list_events("task-1", after_seq=0, limit=10)[0]["seq"] == 1
    assert reloaded.get_task("task-1")["artifact_manifest"] == "result.json"
    assert reloaded.task_path("task-1", "result.json").is_file()
```

- [ ] **Step 2: Run the new tests and verify the expected red failure**

Run: `pytest tests/unit/test_task_store.py -q`

Expected: FAIL because `bridge.store` and the new `TaskStore.create_task/get_task/update_task/transition_task/list_events/save_artifacts` APIs do not exist yet.

- [ ] **Step 3: Define task state and snapshot models**

Create `bridge/store/models.py` with a `TaskState` string enum containing `QUEUED`, `PREPARING`, `RUNNING`, `WAITING_REVIEW`, `COMPLETED`, `FAILED`, `INTERRUPTED`, `UNKNOWN`, and `CANCELLED`; a `TASK_TRANSITIONS` mapping containing the approved runtime transitions plus recovery and existing interrupt/retry compatibility; and a `TaskSnapshot` dataclass whose `to_dict()` includes the required `task_id`, `project`, `task_type`, `state`, timestamps, IDs, `worktree`, `events_file`, `artifact_manifest`, stage, changed files, event cursor, and private recovery/task input fields.

- [ ] **Step 4: Implement canonical TaskStore persistence**

Implement `bridge/store/task_store.py` so that:

1. Safe task IDs use the existing identifier rule and integer issue IDs remain supported.
2. `create_task()` writes `task.json`, `state.json`, `task.yaml`, empty `events.jsonl/stdout.log/stderr.log`, and `{}` `result.json` before returning.
3. `get_task()` reads `task.json` and merges current compatibility fields from `state.json`; old tasks without `task.json` are projected from `state.json` rather than rejected.
4. `update_task()` updates metadata and compatibility state atomically in both snapshots without importing EventBus.
5. `transition_task()` checks the stored `from_state` and `TASK_TRANSITIONS`, then updates state/status/stage/current action/reason atomically; it remains a low-level store primitive so the dependency direction stays `EventBus -> TaskStore`.
6. `append_event()`, `list_events()`, and `read_events()` keep JSONL pagination, tolerate old events, and preserve safe event fields.
7. `save_artifacts()` writes only a bounded manifest to `result.json` and updates `artifact_manifest`; it accepts `path`, `size` or legacy `bytes`, `kind`, and optional `hash`/`source` scalar fields.
8. `_write_json()` uses a sibling temporary file, `flush()`, `os.fsync()`, and `os.replace()`; no `task.json` write path uses `open(..., "w")` directly.

- [ ] **Step 5: Preserve all V0.2 imports and run store tests green**

Make `bridge/task_store.py` re-export `TaskStore` and `utc_now` from `bridge.store.task_store`, or retain the existing class there while exporting the canonical package implementation, so imports in GitHub transport, executors, Codex session, and existing tests keep working. Run: `pytest tests/unit/test_task_store.py tests/test_task_store.py -q`. Expected: PASS.

- [ ] **Step 6: Commit the store slice**

```powershell
git add bridge/store bridge/task_store.py tests/unit/test_task_store.py tests/test_task_store.py
git -c user.name='AI Project Bridge' -c user.email='ai-project-bridge@localhost' commit -m "feat: add durable v0.2.1 task store"
```

### Task 2: Make EventBus the state/event write boundary

**Files:**
- Modify: `bridge/orchestration/event_bus.py`
- Modify: `bridge/orchestration/state_machine.py`
- Modify: `bridge/codex/session.py`
- Test: `tests/unit/test_event_bus.py`
- Modify: `tests/test_v02_codex.py` only to assert the new `time` field where useful

- [ ] **Step 1: Write the failing EventBus and state-machine tests**

Add tests with explicit sequence and transition expectations:

```python
def test_event_bus_assigns_monotonic_sequences_and_persists_snapshot_cursor(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")

    first = bus.emit("task_created", {"project": "demo"})
    second = bus.emit("agent_message", {"message": "safe summary", "reasoning": "remove this"})

    assert [first["seq"], second["seq"]] == [1, 2]
    assert first["task_id"] == "task-1" and "time" in first
    assert "reasoning" not in json.dumps(second)
    assert store.get_task("task-1")["last_event_seq"] == 2
    assert [item["seq"] for item in store.list_events("task-1", after_seq=1, limit=10)] == [2]


def test_event_bus_transition_validates_and_persists_state_changed_event(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("task-1", project="demo", task_type="code", instruction="wait")
    bus = TaskEventBus(store, "task-1")

    bus.transition("QUEUED", "PREPARING", "worker accepted task")

    assert store.get_task("task-1")["state"] == "PREPARING"
    event = store.list_events("task-1", after_seq=0, limit=10)[0]
    assert event["type"] == "state_changed"
    assert event["data"]["from"] == "QUEUED"
```

- [ ] **Step 2: Run tests and verify the expected red failure**

Run: `pytest tests/unit/test_event_bus.py -q`

Expected: FAIL because `TaskEventBus.transition()` and the `time` event field are not implemented.

- [ ] **Step 3: Implement one-way EventBus persistence and safe sanitization**

Keep `event_bus.py` importing the store but ensure the store package never imports EventBus. Implement `emit()` under the existing per-task re-entrant lock: read the snapshot cursor, assign `seq`, emit `seq/task_id/type/time/data`, append through `TaskStore`, then update only the snapshot cursor through `TaskStore.update_task()`. Add `transition(from_state, to_state, reason, **updates)` to validate `from_state` against the current snapshot, validate the state machine transition, update the state/stage/reason through `TaskStore.transition_task()`, and emit one `state_changed` event with the resulting sequence. Keep bounded recursive sanitization and redact reasoning/chain-of-thought keys while retaining agent messages and bounded summaries.

- [ ] **Step 4: Extend the state machine for UNKNOWN without weakening controls**

Add `UNKNOWN` and `CANCELLED` to the session/state enum compatibility path and allow recovery transitions `PREPARING/RUNNING -> UNKNOWN`, `UNKNOWN -> RUNNING/CANCELLED`, while retaining current valid `interrupt`, `retry`, and review transitions. Keep MCP control actions limited to `steer`, `interrupt`, `continue`, and `accept`; no UNKNOWN action is exposed.

- [ ] **Step 5: Route Codex session state transitions through EventBus**

Update `CodexSessionManager._set_state()` to call `TaskEventBus.transition()` when a store-backed session changes state, capture the returned event sequence, and use `_persist()` only for the resulting snapshot/metadata. Update `_fail()` to record the safe error and use the same transition boundary when possible. Keep the existing FakeAppServer injection seam, app-server protocol, workspace-write settings, and `shell=False` untouched. Existing session behavior without a store remains in-memory compatible.

- [ ] **Step 6: Run the focused Codex/event tests green**

Run: `pytest tests/unit/test_event_bus.py tests/test_v02_codex.py -q`. Expected: PASS, with monotonically persisted events and no raw reasoning in event data.

- [ ] **Step 7: Commit the EventBus slice**

```powershell
git add bridge/orchestration/event_bus.py bridge/orchestration/state_machine.py bridge/codex/session.py tests/unit/test_event_bus.py tests/test_v02_codex.py
git -c user.name='AI Project Bridge' -c user.email='ai-project-bridge@localhost' commit -m "feat: route task state through event bus"
```

### Task 3: Add shared WorkerQueue and TaskRunner routing

**Files:**
- Create: `bridge/orchestration/worker.py`
- Create: `bridge/orchestration/task_runner.py`
- Modify: `bridge/orchestration/__init__.py`
- Modify: `bridge/executors/code.py`
- Test: `tests/unit/test_worker_queue.py`
- Test: `tests/unit/test_task_runner.py`

- [ ] **Step 1: Write failing WorkerQueue tests**

Add tests proving that the abstraction owns futures and exceptions:

```python
def test_worker_queue_submits_callable_and_exposes_future(tmp_path):
    queue = WorkerQueue(max_workers=1)
    try:
        future = queue.submit("task-1", lambda: "done")
        assert future.result(timeout=2) == "done"
        assert queue.status("task-1")["state"] == "COMPLETED"
    finally:
        queue.shutdown(wait=True)


def test_worker_queue_preserves_worker_exception_on_future(tmp_path):
    queue = WorkerQueue(max_workers=1)
    try:
        future = queue.submit("task-1", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        with pytest.raises(RuntimeError, match="boom"):
            future.result(timeout=2)
        assert queue.status("task-1")["state"] == "FAILED"
    finally:
        queue.shutdown(wait=True)
```

- [ ] **Step 2: Run the worker tests and verify red**

Run: `pytest tests/unit/test_worker_queue.py -q`

Expected: FAIL because `WorkerQueue` is not defined.

- [ ] **Step 3: Implement WorkerQueue with ThreadPoolExecutor only in one module**

Implement `WorkerQueue` with an injected or internally-created `ThreadPoolExecutor`, a lock-protected task-id-to-future map, `submit(task_id, callable, *args, **kwargs)`, `future(task_id)`, `status(task_id)`, and `shutdown(wait=True, cancel_futures=False)`. Reject duplicate active task IDs, retain completed futures long enough for status inspection, and let the future carry exceptions. No task-specific logic or MCP/Supervisor imports belong in this module.

- [ ] **Step 4: Write failing TaskRunner lifecycle tests**

Create a store-backed fake handler that records execution and either returns or raises. Assert that `TaskRunner.run()` moves a queued task to preparation, lets the handler complete to review, and converts a handler exception to a persisted error plus `FAILED`, then re-raises so WorkerQueue captures it.

- [ ] **Step 5: Implement TaskHandler protocol and TaskRunner**

Define:

```python
class TaskHandler(Protocol):
    def execute(self, task: dict[str, Any]) -> Any: ...


class CodeTaskHandler:
    def __init__(self, executor: TaskHandler):
        self.executor = executor

    def execute(self, task: dict[str, Any]) -> Any:
        return self.executor.execute(task)
```

Implement `TaskRunner.run(task_id)` to load the task, route only `code` in this iteration, transition `QUEUED -> PREPARING`, invoke the handler, ensure a non-session fake handler ends at `WAITING_REVIEW` when it returns, and handle all exceptions in one `except` block. The exception path emits a bounded `error` event, transitions any recoverable current state to `FAILED`, stores only a traceback summary (limited to a few frames/characters), and re-raises the exception. Define explicit unsupported handlers for future experiment-review/presentation routing without invoking those paths here.

- [ ] **Step 6: Add runtime code executor without changing the existing GitHub executor behavior**

Add a small `RuntimeCodeExecutor` in `bridge/executors/code.py`. It accepts the config, `TaskStore`, `CodexSessionManager`, and a supervisor-provided worktree preparation callback. Its `execute(task)` prepares and validates the Bridge worktree, starts a run record, calls `session_manager.start_task()` and `wait_for_completion()`, persists thread/turn/changed-file metadata, finishes the run record, and returns the bounded status/result. It never commits, pushes, invokes GitHub, or marks the task `FAILED`; errors are re-raised to TaskRunner. Existing `CodeExecutor.execute()` and GitHub workflows remain unchanged.

- [ ] **Step 7: Run WorkerQueue and TaskRunner tests green**

Run: `pytest tests/unit/test_worker_queue.py tests/unit/test_task_runner.py -q`. Expected: PASS.

- [ ] **Step 8: Commit the worker slice**

```powershell
git add bridge/orchestration/worker.py bridge/orchestration/task_runner.py bridge/orchestration/__init__.py bridge/executors/code.py tests/unit/test_worker_queue.py tests/unit/test_task_runner.py
git -c user.name='AI Project Bridge' -c user.email='ai-project-bridge@localhost' commit -m "feat: add shared async task runner"
```

### Task 4: Make Supervisor startup asynchronous and add recovery

**Files:**
- Modify: `bridge/orchestration/supervisor.py`
- Modify: `bridge/mcp/tools.py` only if result shaping needs adjustment
- Modify: `bridge/mcp/schemas.py` only if new unit coverage finds a missing validation
- Test: `tests/unit/test_supervisor_async.py`
- Test: `tests/unit/test_mcp_tools.py`
- Modify: `tests/test_v02_mcp.py` to replace the obsolete synchronous `WAITING_REVIEW` startup assertion with queued/immediate semantics

- [ ] **Step 1: Write failing immediate-start and recovery tests**

Use an injected `WorkerQueue`/fake queue and existing FakeAppServer seam so the immediate response is deterministic:

```python
def test_start_code_task_returns_queued_before_worker_runs(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path, execute_immediately=False)

    response = supervisor.start_code_task("demo", "make a safe change")

    assert response["state"] == "QUEUED"
    assert response["project"] == "demo"
    assert store.get_task(response["task_id"])["state"] == "QUEUED"
    assert queue.submitted_task_ids == [response["task_id"]]


def test_worker_later_advances_fake_code_task_to_review(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path, execute_immediately=False)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    queue.run_submitted(task_id)

    assert store.get_task(task_id)["state"] == "WAITING_REVIEW"
    assert store.get_task(task_id)["thread_id"] == "fake-thread"


def test_worker_failure_is_persisted_as_failed(tmp_path):
    supervisor, queue, store = make_supervisor(tmp_path, fake_error=RuntimeError("codex unavailable"), execute_immediately=False)
    task_id = supervisor.start_code_task("demo", "make a safe change")["task_id"]

    with pytest.raises(RuntimeError, match="codex unavailable"):
        queue.run_submitted(task_id)

    assert store.get_task(task_id)["state"] == "FAILED"
    assert any(item["type"] == "error" for item in store.list_events(task_id, after_seq=0, limit=100))


def test_restart_marks_unverifiable_active_task_unknown_and_requeues_queued(tmp_path):
    store = TaskStore(tmp_path / ".bridge")
    store.create_task("queued", project="demo", task_type="code", instruction="queued")
    store.create_task("running", project="demo", task_type="code", instruction="running")
    store.update_task("running", state="RUNNING", status="RUNNING", thread_id="thread-1")

    supervisor, queue, _ = make_supervisor(tmp_path, store=store, execute_immediately=False)

    assert queue.submitted_task_ids == ["queued"]
    assert store.get_task("running")["state"] == "UNKNOWN"
```

- [ ] **Step 2: Run the new supervisor/MCP tests and verify red**

Run: `pytest tests/unit/test_supervisor_async.py tests/unit/test_mcp_tools.py -q`

Expected: FAIL because Supervisor still executes synchronously and has no shared queue/runner/recovery integration.

- [ ] **Step 3: Inject WorkerQueue, RuntimeCodeExecutor, and TaskRunner into Supervisor**

Extend `TaskSupervisor.__init__` with optional `worker_queue`, `task_runner`, and `code_executor` seams. Construct the shared queue, runtime executor, `CodeTaskHandler`, and `TaskRunner` once when they are not injected. Keep existing `session_manager`, `store`, `worktree_factory`, experiment-review, presentation, and GitHub-compatible methods intact.

- [ ] **Step 4: Change only code-task startup to queued submission**

Refactor `start_code_task()` to validate task type/project/capability/instruction, call `_create_task()` before any worker submission, submit `self.task_runner.run(task_id)` once, and return exactly `{"task_id": task_id, "state": "QUEUED", "project": project}`. Do not wait for worktree, Codex process, model list, thread start, turn start, or completion. Keep `start_presentation_task()` and `start_experiment_review()` on their existing paths; `start_task()` dispatches code requests to this asynchronous method without making MCP handle anything directly.

- [ ] **Step 5: Make task creation and transitions event-backed**

Update `_create_task()` to call `TaskStore.create_task()` with the private queued task input needed for execution/recovery, then emit `task_created` and initial `state_changed` through `TaskEventBus`. Replace Supervisor state writes that change state with `bus.transition()`; metadata-only updates may continue through `TaskStore.update_task()`. Ensure initial `task.json` exists before `WorkerQueue.submit()`.

- [ ] **Step 6: Make status/events/artifacts read the durable snapshot**

Change `TaskSupervisor.status()` to read the Store snapshot and return only the public bounded fields: `task_id`, `state`, `stage`, `thread_id`, `turn_id`, `changed_files`, and `last_event_seq`, retaining existing safe fields such as `project`, `review_ready`, model, and last error where already exposed. Keep `task_events()` on Store pagination and `task_artifacts()` on the bounded manifest.

- [ ] **Step 7: Add startup recovery and clean shutdown**

Add `recover_tasks()` and call it after Supervisor runtime construction. Re-submit only persisted `QUEUED` code tasks with sufficient stored input. For persisted `PREPARING/RUNNING` tasks, do not resume in V0.2.1: use the trusted worktree/thread checks as a diagnostic, then transition unverifiable tasks to `UNKNOWN` and persist the reason. Do not expose UNKNOWN control actions. Extend `close()` to shut down the shared WorkerQueue after closing active sessions.

- [ ] **Step 8: Run focused tests and update stale V0.2 assertions**

Run: `pytest tests/unit/test_supervisor_async.py tests/unit/test_mcp_tools.py tests/test_v02_mcp.py -q`. Expected: PASS, with MCP start assertions expecting immediate `QUEUED`; tests that need review call a deterministic queue drain/wait helper before status/control assertions.

- [ ] **Step 9: Commit async Supervisor/MCP integration**

```powershell
git add bridge/orchestration/supervisor.py bridge/mcp/tools.py bridge/mcp/schemas.py tests/unit/test_supervisor_async.py tests/unit/test_mcp_tools.py tests/test_v02_mcp.py
git -c user.name='AI Project Bridge' -c user.email='ai-project-bridge@localhost' commit -m "feat: start code tasks asynchronously"
```

### Task 5: Add Fake smoke coverage and the manual Real Codex smoke script

**Files:**
- Create: `tests/smoke/__init__.py`
- Create: `tests/smoke/test_fake_codex_app_server.py`
- Modify: `bridge/codex/fake_app_server.py` only to add a descriptive `FakeCodexAppServer` alias if needed
- Create: `scripts/codex_smoke_test.py`
- Modify: `README.md` or `docs/mcp-tools.md` with the manual smoke invocation and safety note

- [ ] **Step 1: Write the failing Fake smoke test**

Create a test that injects the existing FakeAppServer through `CodexSessionManager`, runs one queued code task through the real Worker/Runner/Supervisor chain, waits for its future, and asserts:

```python
assert store.get_task(task_id)["state"] == "WAITING_REVIEW"
assert store.get_task(task_id)["thread_id"] == "fake-thread"
assert store.get_task(task_id)["turn_id"] == "fake-turn-1"
assert {item["type"] for item in store.list_events(task_id, after_seq=0, limit=100)} >= {
    "task_created", "state_changed", "thread_started", "turn_started", "agent_message", "turn_completed"
}
```

- [ ] **Step 2: Run the smoke test and verify red**

Run: `pytest tests/smoke/test_fake_codex_app_server.py -q`

Expected: FAIL until the complete asynchronous chain is wired.

- [ ] **Step 3: Implement only the Fake smoke adapter needed by the test**

Reuse the current FakeAppServer initialize/model/list/thread/start/turn/start/control and notification behavior. Add only a compatibility alias named `FakeCodexAppServer` if the smoke test or public documentation needs the longer name; do not add real-process behavior to the fake.

- [ ] **Step 4: Implement the manual Real smoke script**

`scripts/codex_smoke_test.py` must:

1. discover `codex` with the existing executable-discovery seam or `shutil.which` without reading auth/config files;
2. create `tempfile.TemporaryDirectory(prefix="bridge-codex-smoke-")`;
3. instantiate `CodexAppServerClient` with that directory and the discovered executable;
4. perform `initialize`, `model/list`, `thread/start`, and `turn/start` for `Create hello.py with a hello function and add a small test.`;
5. poll only bounded notifications until `turn/completed` or a timeout, collecting `thread_id`, `turn_id`, safe events, and files newly created under the temporary directory;
6. print a concise JSON summary and return a nonzero code on discovery/protocol/turn failure;
7. always close the client and never use Git, the real project path, push/commit operations, secrets, or pytest auto-discovery.

- [ ] **Step 5: Run Fake smoke and script syntax checks**

Run: `pytest tests/smoke/test_fake_codex_app_server.py -q` and `python -m py_compile scripts/codex_smoke_test.py`. Expected: Fake smoke PASS; syntax check exit 0. Do not invoke the real smoke script automatically.

- [ ] **Step 6: Commit smoke coverage**

```powershell
git add tests/smoke scripts/codex_smoke_test.py README.md docs/mcp-tools.md bridge/codex/fake_app_server.py
git -c user.name='AI Project Bridge' -c user.email='ai-project-bridge@localhost' commit -m "test: add fake and real codex smoke coverage"
```

### Task 6: Full verification and acceptance report

**Files:**
- Modify: none unless verification reveals a regression

- [ ] **Step 1: Run the complete pytest suite**

Run: `pytest -q`

Expected: exit code 0 with zero failures and zero errors.

- [ ] **Step 2: Inspect the final diff and safety-sensitive invariants**

Run:

```powershell
git status --short
git diff HEAD~4..HEAD --check
rg -n "ThreadPoolExecutor|threading\.Thread|shell=False|workspace-write|approvalPolicy|open\(.*task\.json.*w" bridge scripts tests
```

Confirm that new application-level worker threads appear only behind `WorkerQueue`, existing Codex protocol reader threads remain unchanged, `shell=False` and workspace-write remain present, and no direct task.json write bypasses atomic persistence.

- [ ] **Step 3: Verify the six acceptance questions from persisted behavior**

Use the Fake smoke test and unit results to report: MCP enters Supervisor; Supervisor submits TaskRunner/creates a Codex session; thread/turn IDs are present in `task.json`/status; event pagination uses `after_seq`; a new TaskStore reads the same snapshot; Fake and manual Real smoke entry points exist. Explicitly list the remaining un-migrated experiment-review, presentation, WPS, and GitHub realtime dashboard modules.

- [ ] **Step 4: Run the final status check before claiming completion**

Run: `git status --short --branch` and record the exact pytest result, changed files, Fake smoke result, Real smoke invocation, and any remaining uncommitted changes. Do not claim completion without the fresh pytest output.

