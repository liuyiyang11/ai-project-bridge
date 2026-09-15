from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from ..codex.session import CodexSessionManager, SessionState, SessionTransitionError
from ..collectors.artifact_collector import collect_artifacts
from ..commands import run_registered_command
from ..executors.code import RuntimeCodeExecutor
from ..security import SecurityError, ensure_safe_relative_path, resolve_under
from ..task_store import TaskStore, utc_now
from ..worktree import WorktreeManager
from .event_bus import TaskEventBus
from .router import TaskRequest, TaskRouter
from .state_machine import InvalidTaskTransition, TaskStateMachine
from .task_runner import CodeTaskHandler, TaskRunner
from .worker import WorkerQueue


class TaskSupervisor:
    """Transport-independent research task orchestration boundary."""

    def __init__(
        self,
        config: Any,
        *,
        store: Optional[TaskStore] = None,
        session_manager: Optional[CodexSessionManager] = None,
        worktree_factory: Optional[Callable[..., Any]] = None,
        worker_queue: Optional[WorkerQueue] = None,
        task_runner: Optional[TaskRunner] = None,
        code_executor: Optional[Any] = None,
    ):
        self.config = config
        self.store = store or TaskStore(config.state_root)
        self.session_manager = session_manager or CodexSessionManager(config, store=self.store)
        self.worktree_factory = worktree_factory
        self.worker_queue = worker_queue or WorkerQueue()
        self._code_executor = code_executor or RuntimeCodeExecutor(
            config,
            self.store,
            self.session_manager,
            self._prepare_worktree_for_task,
        )
        self.task_runner = task_runner or TaskRunner(
            store=self.store,
            handlers={"code": CodeTaskHandler(self._code_executor)},
        )
        self.reconcile_task_events()
        self.recover_tasks()

    def start_task(self, request: TaskRequest, *, worktree: Optional[Path] = None) -> dict[str, Any]:
        TaskRouter.route(request)
        if request.task_type == "code":
            return self._enqueue_code_task(request, worktree=worktree)
        project = self._project_for(request.project, request.task_type)
        if request.task_type == "experiment-review":
            return self.start_experiment_review(
                request.project,
                request.command_id or "",
                source_task_id=request.source_task_id,
                review=request.review,
                task_id=request.task_id,
            )
        if not isinstance(request.instruction, str) or not request.instruction.strip():
            raise ValueError("instruction must be non-empty")
        task_id = request.task_id or self._new_task_id()
        state = self._create_task(task_id, request, project)
        try:
            self._transition(task_id, SessionState.PREPARING, "preparing isolated worktree")
            worktree_info = self._prepare_worktree(project, request.project, task_id, worktree)
            candidate = Path(getattr(worktree_info, "path", worktree_info)).resolve()
            if not candidate.is_dir():
                raise RuntimeError(f"prepared worktree does not exist: {candidate}")
            self.store.update_state(
                task_id,
                worktree_path=str(candidate),
                branch=getattr(worktree_info, "branch", None),
                base_branch=getattr(worktree_info, "base_branch", None),
                base_head=getattr(worktree_info, "base_head", None),
            )
            run = self.store.begin_run(task_id, "initial")
            events_path = self.store.task_dir(task_id) / run["events_path"]
            record = self.session_manager.start_task(
                task_id,
                request.project,
                candidate,
                request.instruction,
                model=request.model,
                reasoning_effort=request.reasoning_effort,
                events_path=events_path,
            )
            self.store.finish_run(task_id, run["number"], status="running", thread_id=record.thread_id)
            return self.status(task_id)
        except Exception as exc:
            self._fail_task(task_id, exc)
            raise

    def start_code_task(
        self,
        project: str,
        instruction: str,
        acceptance: Optional[list[str]] = None,
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        task_id: Optional[str] = None,
        worktree: Optional[Path] = None,
    ) -> dict[str, Any]:
        request = TaskRequest(
            task_type="code",
            project=project,
            instruction=instruction,
            acceptance=acceptance or [],
            model=model,
            reasoning_effort=reasoning_effort,
            task_id=task_id,
        )
        return self._enqueue_code_task(request, worktree=worktree)

    def start_presentation_task(
        self,
        project: str,
        instruction: str,
        *,
        brief: Optional[str] = None,
        slides_spec: Optional[str] = None,
        assets_dir: Optional[str] = None,
        template: Optional[str] = None,
        renderer: str = "auto",
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        task_id: Optional[str] = None,
        worktree: Optional[Path] = None,
    ) -> dict[str, Any]:
        if renderer not in {"auto", "wps", "powerpoint", "libreoffice"}:
            raise ValueError("renderer must be auto, wps, powerpoint, or libreoffice")
        for name, value in {"brief": brief, "slides_spec": slides_spec, "assets_dir": assets_dir, "template": template}.items():
            if value is not None:
                ensure_safe_relative_path(value)
        details = [instruction.strip()]
        for label, value in (("brief", brief), ("slides_spec", slides_spec), ("assets_dir", assets_dir), ("template", template)):
            if value:
                details.append(f"{label}: {value}")
        return self.start_task(
            TaskRequest(
                task_type="presentation",
                project=project,
                instruction="\n\n".join(details),
                model=model,
                reasoning_effort=reasoning_effort,
                task_id=task_id,
                brief=brief,
                slides_spec=slides_spec,
                assets_dir=assets_dir,
                template=template,
                renderer=renderer,
            ),
            worktree=worktree,
        )

    def start_experiment_review(
        self,
        project: str,
        command_id: str,
        *,
        source_task_id: Optional[str] = None,
        review: Any = None,
        task_id: Optional[str] = None,
    ) -> dict[str, Any]:
        project_config = self._project_for(project, "experiment-review")
        if command_id not in project_config.allowed_commands:
            raise ValueError(f"command is not allowed or not registered: {command_id}")
        task_id = task_id or self._new_task_id()
        request = TaskRequest(task_type="experiment-review", project=project, command_id=command_id, source_task_id=source_task_id, review=review, task_id=task_id)
        self._create_task(task_id, request, project_config)
        try:
            self._transition(task_id, SessionState.PREPARING, "preparing experiment review")
            evaluate_root = self._source_worktree(project, source_task_id) if source_task_id else project_config.root
            if not evaluate_root.is_dir():
                raise RuntimeError(f"experiment evaluation root does not exist: {evaluate_root}")
            self._transition(task_id, SessionState.RUNNING, "running registered experiment command")
            self._bus(task_id).emit("experiment_started", {"command_id": command_id})
            result = run_registered_command(project_config, command_id, evaluate_root, self.config.python_executable)
            self.store.append_stdout(task_id, result.stdout or "")
            self.store.append_stderr(task_id, result.stderr or "")
            tests = [{"command_id": command_id, "returncode": result.returncode, "stdout": (result.stdout or "")[-4000:], "stderr": (result.stderr or "")[-4000:]}]
            if result.returncode != 0:
                raise RuntimeError(f"configured experiment command failed with code {result.returncode}")
            bundle_dir = self.store.task_dir(task_id) / "review_bundle"
            artifacts = collect_artifacts(evaluate_root, project_config.artifact_dirs, bundle_dir, self.config.limits)
            summary = bundle_dir / "summary.md"
            summary.write_text(
                f"# Experiment review\n\nCommand: `{command_id}`\n\nCollected artifacts: {len(artifacts)}\n\n"
                + ("\n".join(f"- `{item['source']}` ({item['bytes']} bytes)" for item in artifacts) or "No bounded artifacts were eligible for collection.")
                + "\n",
                encoding="utf-8",
            )
            artifact_list = artifacts + [{"path": "summary.md", "kind": "summary"}]
            self.store.write_result(task_id, {"status": "WAITING_REVIEW", "tests": tests, "artifact_list": artifact_list, "bundle_dir": str(bundle_dir), "source_task_id": source_task_id, "review": TaskEventBus._sanitize(review)})
            self._bus(task_id).emit("experiment_completed", {"command_id": command_id, "artifact_count": len(artifacts)})
            self._transition(task_id, SessionState.WAITING_REVIEW, "review ready")
            return self.status(task_id)
        except Exception as exc:
            self._fail_task(task_id, exc)
            raise

    def control_task(self, task_id: str, action: str, instruction: Optional[str] = None) -> dict[str, Any]:
        if action not in {"steer", "interrupt", "continue", "accept"}:
            raise ValueError("unsupported task action")
        if action in {"steer", "continue"} and (not isinstance(instruction, str) or not instruction.strip()):
            raise ValueError(f"{action} requires a non-empty instruction")
        if action == "accept":
            try:
                record = self.session_manager.accept_task(task_id)
                return record.public_dict()
            except SessionTransitionError:
                state = self._stored_state(task_id)
                if state.get("state", state.get("status")) != SessionState.WAITING_REVIEW.value:
                    raise
                self._transition(task_id, SessionState.COMPLETED, "accepted")
                return self.status(task_id)
        if action == "continue":
            try:
                self.session_manager.continue_task(task_id, instruction or "")
            except SessionTransitionError:
                self.session_manager.restart_task(task_id, instruction or "")
        elif action == "steer":
            self.session_manager.steer_task(task_id, instruction or "")
        else:
            self.session_manager.interrupt_task(task_id)
        return self.status(task_id)

    def task_status(self, task_id: str) -> dict[str, Any]:
        return self.status(task_id)

    def status(self, task_id: str) -> dict[str, Any]:
        state = self._stored_state(task_id)
        return {
            "task_id": task_id,
            "project": state.get("project"),
            "state": state.get("state", state.get("status", "UNKNOWN")),
            "stage": state.get("stage", state.get("current_action", "")),
            "thread_id": state.get("thread_id"),
            "turn_id": state.get("turn_id"),
            "current_action": state.get("current_action", state.get("stage", "")),
            "changed_files": list(state.get("changed_files", [])),
            "review_ready": bool(state.get("review_ready", False)),
            "last_event_seq": int(state.get("last_event_seq", state.get("event_seq", 0))),
            "model": state.get("model"),
            "reasoning_effort": state.get("reasoning_effort"),
            "last_error": state.get("last_error"),
        }

    def task_events(self, task_id: str, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        self._stored_state(task_id)
        return self.store.read_events(task_id, after_seq=after_seq, limit=limit)

    def task_artifacts(self, task_id: str, *, kind: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        if int(limit) < 1 or int(limit) > 1000:
            raise ValueError("limit must be between 1 and 1000")
        self._stored_state(task_id)
        result_path = self.store.task_path(task_id, "result.json")
        if not result_path.is_file():
            return []
        value = json.loads(result_path.read_text(encoding="utf-8"))
        artifacts = value.get("artifact_list", []) if isinstance(value, dict) else []
        if not isinstance(artifacts, list):
            return []
        output: list[dict[str, Any]] = []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            if kind and kind != "all" and item.get("kind") != kind:
                continue
            safe = {key: item[key] for key in ("path", "kind", "size", "hash") if key in item and isinstance(item[key], (str, int, float, bool))}
            if "size" not in safe and isinstance(item.get("bytes"), (str, int, float, bool)):
                safe["size"] = item["bytes"]
            if "path" in safe and isinstance(safe["path"], str) and (Path(safe["path"]).is_absolute() or ".." in safe["path"].replace("\\", "/").split("/")):
                continue
            output.append(safe)
            if len(output) >= int(limit):
                break
        return output

    def list_projects(self) -> list[dict[str, Any]]:
        return [{"id": name, "capabilities": list(project.capabilities)} for name, project in sorted(self.config.projects.items())]

    def codex_catalog(self) -> dict[str, Any]:
        configured = next(iter(self.config.projects.values())).root if self.config.projects else Path.cwd()
        worktree = configured if Path(configured).is_dir() else Path.cwd()
        return self.session_manager.get_catalog(worktree).public_dict()

    def close(self) -> None:
        self.session_manager.close()
        self.worker_queue.shutdown(wait=False, cancel_futures=True)

    def _enqueue_code_task(self, request: TaskRequest, *, worktree: Optional[Path] = None) -> dict[str, Any]:
        TaskRouter.route(request)
        project = self._project_for(request.project, "code")
        if not isinstance(request.instruction, str) or not request.instruction.strip():
            raise ValueError("instruction must be non-empty")
        task_id = request.task_id or self._new_task_id()
        self._create_task(task_id, request, project)
        if worktree is not None:
            self.store.update_task(task_id, supplied_worktree=str(Path(worktree).resolve()))
        try:
            self.worker_queue.submit(task_id, self.task_runner.run, task_id)
        except Exception as exc:
            self._fail_task(task_id, exc)
            raise
        return {"task_id": task_id, "state": SessionState.QUEUED.value, "project": request.project}

    def recover_tasks(self) -> None:
        """Requeue durable queued work and make unverifiable active work explicit."""
        for task_id in self.store.list_task_ids():
            try:
                state = self.store.get_task(task_id)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            task_state = state.get("state", state.get("status"))
            if task_state == SessionState.QUEUED.value and state.get("task_type", "code") == "code":
                try:
                    self.worker_queue.submit(task_id, self.task_runner.run, task_id)
                except ValueError:
                    continue
            elif task_state in {SessionState.PREPARING.value, SessionState.RUNNING.value}:
                self._bus(task_id).transition(
                    str(task_state),
                    SessionState.UNKNOWN.value,
                    "Bridge restarted before active Codex session could be verified",
                )

    def reconcile_task_events(self) -> None:
        for task_id in self.store.list_task_ids():
            try:
                self._bus(task_id).reconcile()
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def _prepare_worktree_for_task(self, project_name: str, task_id: str) -> Any:
        state = self._stored_state(task_id)
        project = self._project_for(project_name, "code")
        supplied = state.get("supplied_worktree")
        return self._prepare_worktree(project, project_name, task_id, Path(supplied) if isinstance(supplied, str) else None)

    def _create_task(self, task_id: str, request: TaskRequest, project: Any) -> dict[str, Any]:
        if self.store.exists(task_id):
            raise ValueError(f"task already exists: {task_id}")
        if not isinstance(request.acceptance, list):
            request.acceptance = []
        self.store.create_task(
            task_id,
            project=request.project,
            task_type=request.task_type,
            instruction=request.instruction,
            acceptance=request.acceptance,
            model=request.model,
            reasoning_effort=request.reasoning_effort,
        )
        bus = self._bus(task_id)
        bus.emit("task_created", {"project": request.project, "task_type": request.task_type})
        bus.emit("state_changed", {"from": None, "to": SessionState.QUEUED.value, "reason": "queued"})
        return self.store.get_task(task_id)

    def _prepare_worktree(self, project: Any, project_name: str, task_id: str, supplied: Optional[Path]) -> Any:
        if supplied is not None:
            candidate = Path(supplied).resolve()
            root = (self.config.state_root / "worktrees").resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise SecurityError("task worktree must be under the Bridge worktree root") from exc
            return candidate
        if self.worktree_factory:
            try:
                return self.worktree_factory(project, task_id)
            except TypeError:
                return self.worktree_factory(project)
        manager = WorktreeManager(project.root, self.config.state_root / "worktrees", project.remote, project.base_branch)
        execution_number = self.store.next_execution_number()
        info = manager.prepare(project_name, execution_number)
        self.store.update_state(task_id, execution_number=execution_number)
        return info

    def _source_worktree(self, project: str, source_task_id: Optional[str]) -> Path:
        if not source_task_id:
            raise ValueError("source_task_id is required for a candidate review")
        source_state = self._stored_state(source_task_id)
        if source_state.get("project") != project:
            raise ValueError("source_task_id belongs to a different project")
        if source_state.get("task_type") != "code":
            raise ValueError("source_task_id must identify a code task")
        value = source_state.get("worktree_path")
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise SecurityError("source task has no trusted absolute worktree")
        candidate = Path(value).resolve()
        root = (self.config.state_root / "worktrees").resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError as exc:
            raise SecurityError("source task worktree is outside the Bridge worktree root") from exc
        trusted = resolve_under(root, relative.as_posix())
        if trusted != candidate or not candidate.is_dir():
            raise SecurityError("source task worktree is not a trusted candidate worktree")
        return candidate

    def _project_for(self, project_name: str, task_type: str) -> Any:
        project = self.config.project(project_name)
        if task_type not in project.capabilities:
            raise ValueError(f"project {project_name!r} does not support task type {task_type!r}")
        return project

    def _bus(self, task_id: str) -> TaskEventBus:
        return TaskEventBus(self.store, task_id)

    def _transition(self, task_id: str, new_state: SessionState, action: str) -> None:
        state = self._stored_state(task_id)
        old_value = state.get("state", state.get("status", SessionState.QUEUED.value))
        old_state = SessionState(old_value)
        if old_state == new_state:
            return
        self._bus(task_id).transition(
            old_state.value,
            new_state.value,
            action,
            updates={"review_ready": new_state == SessionState.WAITING_REVIEW},
        )

    def _fail_task(self, task_id: str, error: Exception) -> None:
        if not self.store.exists(task_id):
            return
        state = self._stored_state(task_id)
        current = state.get("state", state.get("status", SessionState.QUEUED.value))
        message = f"{type(error).__name__}: {error}"[:2000]
        if current != SessionState.FAILED.value and TaskStateMachine.can_transition(current, SessionState.FAILED.value):
            self._bus(task_id).transition(str(current), SessionState.FAILED.value, "failed")
        self.store.update_task(task_id, last_error=message)
        self._bus(task_id).emit("error", {"message": message, "previous_state": current})

    def _stored_state(self, task_id: str) -> dict[str, Any]:
        if not self.store.exists(task_id):
            raise ValueError(f"task does not exist: {task_id}")
        return self.store.load_state(task_id)

    @staticmethod
    def _new_task_id() -> str:
        return "task-" + uuid.uuid4().hex
