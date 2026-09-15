from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional

from ..task_store import TaskStore
from .app_server import CodexAppServerClient
from .model_catalog import CodexModelCatalog, CodexModelError
from .protocol import CodexProcessError, CodexProtocolError
from .runner import CodexResumeMismatchError


class SessionState(str, Enum):
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_REVIEW = "WAITING_REVIEW"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


@dataclass
class SessionRecord:
    task_id: str
    project: str
    worktree: Path
    process: Any = None
    thread_id: Optional[str] = None
    active_turn_id: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    state: SessionState = SessionState.QUEUED
    manage_task_lifecycle: bool = True
    event_seq: int = 0
    current_action: str = "queued"
    changed_files: list[str] = field(default_factory=list)
    final_message: str = ""
    last_error: Optional[str] = None
    events_path: Optional[Path] = None
    completion: threading.Event = field(default_factory=threading.Event, repr=False)
    message_parts: list[str] = field(default_factory=list, repr=False)
    interrupt_requested: bool = field(default=False, repr=False)

    @property
    def turn_id(self) -> Optional[str]:
        return self.active_turn_id

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "project": self.project,
            "state": self.state.value,
            "stage": self.current_action,
            "thread_id": self.thread_id,
            "turn_id": self.active_turn_id,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "current_action": self.current_action,
            "changed_files": list(self.changed_files),
            "review_ready": self.state == SessionState.WAITING_REVIEW,
            "last_event_seq": self.event_seq,
            "final_message": self.final_message,
            "last_error": self.last_error,
        }


@dataclass(frozen=True)
class SessionResult:
    task_id: str
    thread_id: Optional[str]
    turn_id: Optional[str]
    exit_code: int
    final_message: str
    stdout: str = ""
    stderr: str = ""


class SessionTransitionError(RuntimeError):
    """Raised when a control operation is invalid for the session state."""


class CodexSessionManager:
    """Own active Codex app-server sessions and their safe lifecycle state."""

    def __init__(
        self,
        config: Any = None,
        *,
        store: Optional[TaskStore] = None,
        client_factory: Optional[Callable[..., Any]] = None,
        event_bus_factory: Optional[Callable[[TaskStore, str], Any]] = None,
        catalog: Optional[CodexModelCatalog] = None,
    ):
        self.config = config
        self.store = store
        self.client_factory = client_factory
        self.event_bus_factory = event_bus_factory
        self.catalog = catalog or CodexModelCatalog()
        self.sessions: dict[str, SessionRecord] = {}
        self._clients: dict[str, Any] = {}
        self._buses: dict[str, Any] = {}
        self._lock = threading.RLock()

    def start_task(
        self,
        task_id: str,
        project: str,
        worktree: Path,
        instruction: str,
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        resume_thread_id: Optional[str] = None,
        events_path: Optional[Path] = None,
        manage_task_lifecycle: bool = True,
    ) -> SessionRecord:
        self._validate_task_id(task_id)
        if not isinstance(instruction, str) or not instruction.strip():
            raise CodexProtocolError("task instruction must be non-empty")
        worktree = Path(worktree).resolve()
        if not worktree.is_dir():
            raise CodexProtocolError(f"task worktree does not exist: {worktree}")
        with self._lock:
            if task_id in self.sessions and self.sessions[task_id].state in {SessionState.RUNNING, SessionState.PREPARING}:
                raise SessionTransitionError(f"task is already active: {task_id}")
            if task_id in self.sessions:
                self._close_client(task_id)
            stored_state = SessionState.QUEUED
            stored_event_seq = 0
            if self.store and self.store.exists(task_id):
                try:
                    snapshot = self.store.get_task(task_id)
                    stored_value = snapshot.get("state", SessionState.QUEUED.value)
                    stored_state = SessionState(stored_value)
                    stored_event_seq = max(0, int(snapshot.get("last_event_seq", snapshot.get("event_seq", 0))))
                except (ValueError, TypeError):
                    stored_state = SessionState.QUEUED
            if not manage_task_lifecycle and stored_state == SessionState.INTERRUPTED:
                raise SessionTransitionError("task was interrupted before Codex session start")
            record = SessionRecord(
                task_id=task_id,
                project=project,
                worktree=worktree,
                state=stored_state,
                manage_task_lifecycle=bool(manage_task_lifecycle),
                event_seq=stored_event_seq,
                current_action="queued" if stored_state == SessionState.QUEUED else "starting app-server",
                model=model,
                reasoning_effort=reasoning_effort,
                events_path=Path(events_path).resolve() if events_path else None,
            )
            self.sessions[task_id] = record
            self._persist(record)
            if record.state == SessionState.QUEUED:
                self._set_state(record, SessionState.PREPARING, "starting app-server")
        try:
            client = self._new_client(record)
            if getattr(client, "notification_handler", None) is None:
                try:
                    client.notification_handler = lambda event: self._on_event(record.task_id, event)
                except Exception:
                    pass
            if getattr(client, "process_error_handler", None) is None:
                try:
                    client.process_error_handler = lambda error: self._on_process_error(record.task_id, error)
                except Exception:
                    pass
            self._clients[task_id] = client
            client.start()
            record.process = getattr(client, "process", None) or client
            catalog_response = client.model_list()
            self.catalog = CodexModelCatalog.from_response(catalog_response)
            policy = getattr(getattr(self.config, "codex", None), "routing_policy", "inherit")
            self.catalog.validate(model, reasoning_effort, policy=policy)
            if resume_thread_id:
                response = client.thread_resume(resume_thread_id, model=model, cwd=worktree)
            else:
                response = client.thread_start(model=model, cwd=worktree)
            returned_thread = self._thread_id(response)
            if not returned_thread:
                raise CodexProtocolError("thread/start or thread/resume returned no thread id")
            if resume_thread_id and returned_thread != resume_thread_id:
                raise CodexResumeMismatchError(resume_thread_id, returned_thread)
            with self._lock:
                record.thread_id = returned_thread
                record.current_action = "starting turn"
                self._persist(record)
            # Set RUNNING before turn/start: a fake or very fast server may
            # emit turn/completed while the request response is still pending.
            self._set_state(record, SessionState.RUNNING, "running turn")
            turn_response = client.turn_start(
                returned_thread,
                instruction,
                model=model,
                reasoning_effort=reasoning_effort,
            )
            turn_id = self._turn_id(turn_response)
            if turn_id:
                with self._lock:
                    record.active_turn_id = turn_id
                    self._persist(record)
            return record
        except Exception as exc:
            self._close_client(task_id)
            self._fail(record, exc)
            raise

    def resume_task(self, task_id: str, project: str, worktree: Path, thread_id: str, instruction: str, **kwargs: Any) -> SessionRecord:
        return self.start_task(
            task_id,
            project,
            worktree,
            instruction,
            resume_thread_id=thread_id,
            **kwargs,
        )

    def start_code_task(self, task_id: str, project: str, worktree: Path, instruction: str, **kwargs: Any) -> SessionRecord:
        return self.start_task(task_id, project, worktree, instruction, **kwargs)

    def start_presentation_task(self, task_id: str, project: str, worktree: Path, instruction: str, **kwargs: Any) -> SessionRecord:
        return self.start_task(task_id, project, worktree, instruction, **kwargs)

    def continue_task(self, task_id: str, instruction: str) -> SessionRecord:
        record = self._get(task_id)
        self._require_state(record, {SessionState.WAITING_REVIEW}, "continue")
        if not isinstance(instruction, str) or not instruction.strip():
            raise SessionTransitionError("continue requires a non-empty instruction")
        record.completion.clear()
        record.last_error = None
        record.final_message = ""
        record.message_parts.clear()
        self._take_over_lifecycle_for_control(record)
        self._set_state(record, SessionState.RUNNING, "continuing turn")
        try:
            response = self._clients[task_id].turn_start(
                record.thread_id or "",
                instruction,
                model=record.model,
                reasoning_effort=record.reasoning_effort,
            )
            turn_id = self._turn_id(response)
            if turn_id:
                record.active_turn_id = turn_id
                self._persist(record)
            return record
        except Exception as exc:
            self._close_client(task_id)
            self._fail(record, exc)
            raise

    def steer_task(self, task_id: str, instruction: str) -> SessionRecord:
        record = self._get(task_id)
        self._require_state(record, {SessionState.RUNNING}, "steer")
        if not isinstance(instruction, str) or not instruction.strip():
            raise SessionTransitionError("steer requires a non-empty instruction")
        self._clients[task_id].turn_steer(record.thread_id or "", record.active_turn_id or "", instruction)
        record.current_action = "steer sent"
        self._persist(record)
        return record

    def interrupt_task(self, task_id: str) -> SessionRecord:
        record = self._get(task_id)
        with self._lock:
            self._require_state(record, {SessionState.RUNNING}, "interrupt")
            self._take_over_lifecycle_for_control(record)
            # Mark the local terminal intent and persist the terminal state
            # before asking app-server to interrupt.  The server may emit the
            # turn/completed notification synchronously while that request is
            # in flight, so interrupt must win the race by construction.
            record.interrupt_requested = True
            self._set_state(record, SessionState.INTERRUPTED, "interrupted")
            record.completion.set()
            client = self._clients[task_id]
            thread_id = record.thread_id or ""
            turn_id = record.active_turn_id or ""
        client.turn_interrupt(thread_id, turn_id)
        return record

    def accept_task(self, task_id: str) -> SessionRecord:
        record = self._get(task_id)
        self._require_state(record, {SessionState.WAITING_REVIEW}, "accept")
        self._take_over_lifecycle_for_control(record)
        self._set_state(record, SessionState.COMPLETED, "accepted")
        self._close_client(task_id)
        return record

    def wait_for_completion(self, task_id: str, *, timeout: Optional[float] = None) -> SessionResult:
        record = self._get(task_id)
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while not record.completion.is_set():
            client = self._clients.get(task_id)
            drain = getattr(client, "drain_notifications", None)
            if callable(drain):
                drain(limit=100)
            if record.completion.is_set():
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexProtocolError(f"timed out waiting for task turn: {task_id}")
                record.completion.wait(timeout=min(0.05, remaining))
            else:
                record.completion.wait(timeout=0.05)
        if record.state == SessionState.FAILED:
            raise CodexProcessError(record.last_error or "Codex task failed")
        return SessionResult(
            task_id=record.task_id,
            thread_id=record.thread_id,
            turn_id=record.active_turn_id,
            exit_code=0 if record.state == SessionState.WAITING_REVIEW else 130 if record.state == SessionState.INTERRUPTED else 1,
            final_message=record.final_message,
        )

    def run_task(self, task_id: str, project: str, worktree: Path, instruction: str, **kwargs: Any) -> SessionResult:
        timeout = kwargs.pop("timeout", None) if "timeout" in kwargs else None
        self.start_task(task_id, project, worktree, instruction, **kwargs)
        if timeout is None:
            timeout = getattr(getattr(self.config, "codex", None), "turn_timeout_seconds", 86400)
        return self.wait_for_completion(task_id, timeout=float(timeout))

    def restart_task(self, task_id: str, instruction: str) -> SessionRecord:
        if self.store is None:
            raise SessionTransitionError("restart requires a TaskStore")
        state = self.store.load_state(task_id)
        thread_id = state.get("thread_id")
        worktree = state.get("worktree_path")
        project = state.get("project")
        if not all(isinstance(value, str) and value for value in (thread_id, worktree, project)):
            raise SessionTransitionError("saved task state does not contain project, worktree_path, and thread_id")
        if state.get("task_type", "code") != "code":
            raise SessionTransitionError("only code tasks can resume a saved Codex thread")
        project_lookup = getattr(self.config, "project", None)
        if not callable(project_lookup):
            raise SessionTransitionError("saved task project cannot be validated")
        try:
            project_config = project_lookup(project)
        except Exception as exc:
            raise SessionTransitionError("saved task project is not registered") from exc
        if "code" not in getattr(project_config, "capabilities", []):
            raise SessionTransitionError("saved task project does not support code tasks")
        state_root = getattr(self.config, "state_root", None)
        if state_root is None:
            raise SessionTransitionError("saved task worktree cannot be validated")
        worktree_path = Path(worktree).expanduser().resolve()
        worktree_root = (Path(state_root).resolve() / "worktrees").resolve()
        try:
            relative = worktree_path.relative_to(worktree_root)
        except ValueError as exc:
            raise SessionTransitionError("saved task worktree is outside the Bridge worktree root") from exc
        if relative == Path(".") or not worktree_path.is_dir():
            raise SessionTransitionError("saved task worktree is not a valid Bridge worktree")
        return self.start_task(
            task_id,
            project,
            worktree_path,
            instruction,
            model=state.get("model"),
            reasoning_effort=state.get("reasoning_effort"),
            resume_thread_id=thread_id,
        )

    def get_catalog(self, worktree: Optional[Path] = None) -> CodexModelCatalog:
        if self.catalog.models:
            return self.catalog
        if self.sessions:
            client = next(iter(self._clients.values()), None)
            if client is not None:
                self.catalog = CodexModelCatalog.from_response(client.model_list())
                return self.catalog
        if worktree is None:
            worktree = Path.cwd()
        client = self._new_ephemeral_client(Path(worktree).resolve())
        try:
            client.start()
            self.catalog = CodexModelCatalog.from_response(client.model_list())
            return self.catalog
        finally:
            if hasattr(client, "close"):
                client.close()

    def status(self, task_id: str) -> dict[str, Any]:
        return self._get(task_id).public_dict()

    def close(self) -> None:
        for task_id in list(self._clients):
            self._close_client(task_id)

    def _new_client(self, record: SessionRecord) -> Any:
        handler = lambda event: self._on_event(record.task_id, event)
        error_handler = lambda error: self._on_process_error(record.task_id, error)
        if self.client_factory:
            try:
                return self.client_factory(
                    cwd=record.worktree,
                    notification_handler=handler,
                    process_error_handler=error_handler,
                )
            except TypeError:
                try:
                    return self.client_factory(cwd=record.worktree)
                except TypeError:
                    return self.client_factory(record.worktree)
        binary = getattr(self.config, "codex_binary", "codex") if self.config is not None else "codex"
        codex = getattr(self.config, "codex", None)
        return CodexAppServerClient(
            binary,
            cwd=record.worktree,
            request_timeout=getattr(codex, "request_timeout_seconds", 30),
            initialize_timeout=getattr(codex, "initialize_timeout_seconds", 30),
            notification_handler=handler,
            process_error_handler=error_handler,
        )

    def _new_ephemeral_client(self, cwd: Path) -> Any:
        if self.client_factory:
            try:
                return self.client_factory(cwd=cwd)
            except TypeError:
                return self.client_factory(cwd)
        binary = getattr(self.config, "codex_binary", "codex") if self.config is not None else "codex"
        return CodexAppServerClient(binary, cwd=cwd)

    def _on_event(self, task_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            record = self.sessions.get(task_id)
            if record is None:
                return
            if record.events_path:
                record.events_path.parent.mkdir(parents=True, exist_ok=True)
                with record.events_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            method = event.get("method")
            params = event.get("params") if isinstance(event.get("params"), dict) else {}
            # INTERRUPTED/CANCELLED is a terminal boundary for every late
            # app-server notification.  In particular, a completion from the
            # old turn must not mutate memory, durable state, or review_ready.
            if record.interrupt_requested or record.state in {SessionState.INTERRUPTED, SessionState.CANCELLED}:
                return
            if method == "thread/started":
                thread_id = self._thread_id(params)
                if thread_id and record.thread_id is None:
                    record.thread_id = thread_id
            elif method == "turn/started":
                record.active_turn_id = self._turn_id(params)
                record.current_action = "running turn"
            elif method == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    record.message_parts.append(delta[:16000])
                    record.final_message = "".join(record.message_parts)[-50000:]
            elif method in {"turn/completed", "turn/finished"}:
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else params
                record.active_turn_id = self._turn_id(turn) or record.active_turn_id
                status = turn.get("status")
                if not record.final_message:
                    record.final_message = self._message_from_turn(turn)
                if status == "completed" or status is None:
                    self._set_state(record, SessionState.WAITING_REVIEW, "review ready")
                elif status == "interrupted":
                    self._set_state(record, SessionState.INTERRUPTED, "interrupted")
                else:
                    self._fail(record, RuntimeError(self._turn_error(turn) or "Codex turn failed"))
                record.completion.set()
            elif method == "error":
                message = params.get("error") if isinstance(params.get("error"), dict) else params
                self._fail(record, RuntimeError(self._turn_error(message) or str(message)))
            elif method == "warning":
                record.current_action = str(params.get("message") or "warning")[:1000]
            elif method == "thread/status/changed":
                record.current_action = "thread status changed"
            elif method in {"turn/diff/updated", "item/fileChange/outputDelta", "item/fileChange/patchUpdated"}:
                record.current_action = "files changed"
                record.changed_files.extend(path for path in self._changed_paths(params) if path not in record.changed_files)
            elif method == "item/commandExecution/outputDelta":
                record.current_action = "command output"
            self._emit_event(record, method or "notification", params)
            self._persist(record)

    def _on_process_error(self, task_id: str, error: Exception) -> None:
        record = self.sessions.get(task_id)
        if record and record.state in {SessionState.PREPARING, SessionState.RUNNING}:
            self._fail(record, error)

    def _emit_event(self, record: SessionRecord, method: str, params: dict[str, Any]) -> None:
        mapped = {
            "thread/started": "thread_started",
            "turn/started": "turn_started",
            "turn/completed": "turn_completed",
            "item/agentMessage/delta": "agent_message",
            "turn/diff/updated": "file_changed",
            "item/fileChange/outputDelta": "file_changed",
            "item/fileChange/patchUpdated": "file_changed",
            "warning": "warning",
            "error": "error",
            "thread/status/changed": "thread_status_changed",
            "turn/plan/updated": "turn_plan_updated",
        }.get(method, method.replace("/", "_"))
        if method in {"item/started", "item/completed"}:
            item = params.get("item") if isinstance(params.get("item"), dict) else params
            item_type = str(item.get("type", "")) if isinstance(item, dict) else ""
            if "commandExecution" in item_type or "command_execution" in item_type:
                mapped = "command_started" if method == "item/started" else "command_completed"
        bus = self._bus(record)
        if bus is not None:
            emitted = bus.emit(mapped, self._safe_event_data(params), task_id=record.task_id)
            if isinstance(emitted, dict):
                record.event_seq = int(emitted.get("seq", record.event_seq))

    def _bus(self, record: SessionRecord) -> Any:
        if record.task_id in self._buses:
            return self._buses[record.task_id]
        if not self.store:
            return None
        if self.event_bus_factory:
            bus = self.event_bus_factory(self.store, record.task_id)
        else:
            from ..orchestration.event_bus import TaskEventBus

            bus = TaskEventBus(self.store, record.task_id)
        self._buses[record.task_id] = bus
        return bus

    def _persist(self, record: SessionRecord) -> None:
        if not self.store or not self.store.exists(record.task_id):
            return
        updates = {
            "thread_id": record.thread_id,
            "turn_id": record.active_turn_id,
            "model": record.model,
            "reasoning_effort": record.reasoning_effort,
            "changed_files": list(record.changed_files),
            "last_error": record.last_error,
        }
        if record.manage_task_lifecycle:
            updates.update(
                stage=record.current_action,
                current_action=record.current_action,
                review_ready=record.state == SessionState.WAITING_REVIEW,
            )
        self.store.update_task(record.task_id, **updates)

    def _take_over_lifecycle_for_control(self, record: SessionRecord) -> None:
        """Hand durable lifecycle ownership back to EventBus for a control turn."""
        if record.manage_task_lifecycle:
            return
        if self.store is not None and self.store.exists(record.task_id):
            snapshot = self.store.get_task(record.task_id)
            stored_state = str(snapshot.get("state", snapshot.get("status", "")))
            if stored_state != record.state.value:
                raise SessionTransitionError(
                    f"durable task state {stored_state!r} does not match active session state {record.state.value!r}"
                )
            try:
                record.event_seq = max(record.event_seq, int(snapshot.get("last_event_seq", snapshot.get("event_seq", 0))))
            except (TypeError, ValueError):
                pass
        record.manage_task_lifecycle = True

    def _set_state(self, record: SessionRecord, state: SessionState, action: str) -> None:
        with self._lock:
            old = record.state
            emitted = None
            # Validate and persist the durable transition before mutating the
            # in-memory record.  If EventBus rejects the transition, the
            # SessionRecord remains a faithful view of the previous state.
            if old != state:
                # Keep the import local: the state-machine module imports the
                # SessionState enum from this module.
                from ..orchestration.state_machine import TaskStateMachine

                TaskStateMachine.require(old, state)
                if record.manage_task_lifecycle:
                    bus = self._bus(record)
                    if bus is not None:
                        emitted = bus.transition(
                            old.value,
                            state.value,
                            action,
                            updates={"review_ready": state == SessionState.WAITING_REVIEW},
                        )
            record.state = state
            record.current_action = action
            if isinstance(emitted, dict):
                record.event_seq = int(emitted.get("seq", record.event_seq))
            self._persist(record)

    def _fail(self, record: SessionRecord, error: Exception) -> None:
        record.last_error = f"{type(error).__name__}: {error}"
        if record.state != SessionState.FAILED:
            self._set_state(record, SessionState.FAILED, "failed")
        else:
            record.current_action = "failed"
            self._persist(record)
        record.completion.set()
        self._emit_event(record, "error", {"message": record.last_error})
        self._persist(record)

    def _close_client(self, task_id: str) -> None:
        client = self._clients.pop(task_id, None)
        if client is not None and hasattr(client, "close"):
            client.close()

    def _get(self, task_id: str) -> SessionRecord:
        try:
            return self.sessions[task_id]
        except KeyError as exc:
            raise SessionTransitionError(f"task session is not active: {task_id}") from exc

    @staticmethod
    def _require_state(record: SessionRecord, allowed: set[SessionState], action: str) -> None:
        if record.state not in allowed:
            expected = ", ".join(item.value for item in allowed)
            raise SessionTransitionError(f"cannot {action} task in {record.state.value}; expected {expected}")

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        import re

        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise ValueError("task_id must be a safe identifier")

    @staticmethod
    def _thread_id(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            thread = value.get("thread")
            if isinstance(thread, dict) and isinstance(thread.get("id"), str):
                return thread["id"]
            for key in ("threadId", "thread_id", "id"):
                if isinstance(value.get(key), str) and value[key]:
                    return value[key]
        return None

    @staticmethod
    def _turn_id(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            turn = value.get("turn")
            if isinstance(turn, dict) and isinstance(turn.get("id"), str):
                return turn["id"]
            for key in ("turnId", "turn_id", "id"):
                if isinstance(value.get(key), str) and value[key]:
                    return value[key]
        return None

    @staticmethod
    def _message_from_turn(turn: dict[str, Any]) -> str:
        items = turn.get("items") if isinstance(turn, dict) else []
        messages: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") in {"agentMessage", "agent_message"}:
                text = item.get("text") or item.get("message")
                if isinstance(text, str):
                    messages.append(text)
        return "\n".join(messages)[-50000:]

    @staticmethod
    def _turn_error(value: Any) -> str:
        if isinstance(value, dict):
            error = value.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error.get("error") or "")
            if isinstance(value.get("message"), str):
                return value["message"]
        return ""

    @staticmethod
    def _safe_event_data(value: Any) -> Any:
        if isinstance(value, dict):
            item_type = value.get("type")
            if isinstance(item_type, str) and "reasoning" in item_type.casefold():
                return {"type": "reasoning_redacted"}
            blocked = {"reasoning", "rawreasoning", "chainofthought", "textdelta", "raw_chain_of_thought", "raw_reasoning"}
            return {
                str(key): CodexSessionManager._safe_event_data(item)
                for key, item in value.items()
                if str(key).casefold() not in blocked
            }
        if isinstance(value, list):
            return [CodexSessionManager._safe_event_data(item) for item in value[:100]]
        if isinstance(value, str):
            return value[:16000]
        return value if value is None or isinstance(value, (bool, int, float)) else str(value)[:1000]

    @staticmethod
    def _changed_paths(value: Any) -> list[str]:
        from ..security import ensure_safe_relative_path

        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"path", "filePath", "file_path"} and isinstance(item, str):
                    try:
                        safe = ensure_safe_relative_path(item)
                    except Exception:
                        continue
                    if safe not in found:
                        found.append(safe)
                else:
                    found.extend(path for path in CodexSessionManager._changed_paths(item) if path not in found)
        elif isinstance(value, list):
            for item in value[:100]:
                found.extend(path for path in CodexSessionManager._changed_paths(item) if path not in found)
        return found[:100]
