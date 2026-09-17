from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from ..security import validate_unicode_scalars
from .models import TASK_TRANSITIONS, TaskState


logger = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Filesystem-backed task snapshots, events, logs, and manifests."""

    _FILES = ("task.json", "task.yaml", "state.json", "events.jsonl", "stdout.log", "stderr.log", "result.json")
    _STAGING_NAME = re.compile(r"\.task-[A-Za-z0-9][A-Za-z0-9._-]{0,127}-[A-Za-z0-9_-]+\.tmp\Z")
    _CLAIM_NAME = re.compile(r"\.task-[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.claim\Z")
    _task_locks_guard = threading.Lock()
    _task_locks: dict[str, threading.RLock] = {}

    def __init__(self, root: Path):
        self.root = Path(root)

    @staticmethod
    def _task_key(task_id: Union[int, str]) -> str:
        if isinstance(task_id, bool):
            raise ValueError("task id must be a safe identifier")
        if isinstance(task_id, int):
            if task_id <= 0:
                raise ValueError("issue number must be positive")
            return str(task_id)
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", task_id):
            raise ValueError("task id must be a safe identifier")
        return task_id

    def task_dir(self, task_id: Union[int, str]) -> Path:
        return self.root / "tasks" / self._task_key(task_id)

    def task_path(self, task_id: Union[int, str], name: str) -> Path:
        if name not in self._FILES:
            raise ValueError(f"unsupported task file: {name}")
        return self.task_dir(task_id) / name

    def task_lock(self, task_id: Union[int, str]) -> threading.RLock:
        """Return the cross-instance lock for one task's snapshot and journal."""
        key = self._task_key(task_id)
        lock_key = f"{self.root.resolve()}::{key}"
        with self._task_locks_guard:
            return self._task_locks.setdefault(lock_key, threading.RLock())

    def create_task(
        self,
        task_id: Union[int, str],
        *,
        project: str,
        task_type: str = "code",
        instruction: str = "",
        acceptance: Optional[list[str]] = None,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        **metadata: Any,
    ) -> dict[str, Any]:
        key = self._task_key(task_id)
        final_directory = self.task_dir(key)
        if final_directory.exists() or final_directory.is_symlink():
            raise ValueError(f"task already exists: {key}")
        reserved = {
            "state",
            "status",
            "stage",
            "current_action",
            "event_seq",
            "last_event_seq",
            "review_ready",
        }
        if reserved.intersection(metadata):
            raise ValueError("lifecycle fields are initialized by create_task")
        now = utc_now()
        state: dict[str, Any] = {
            "task_id": key,
            "project": project,
            "task_type": task_type,
            "state": TaskState.QUEUED.value,
            "status": TaskState.QUEUED.value,
            "stage": "queued",
            "current_action": "queued",
            "created_at": now,
            "updated_at": now,
            "thread_id": None,
            "turn_id": None,
            "worktree": None,
            "worktree_path": None,
            "events_file": "events.jsonl",
            "artifact_manifest": "result.json",
            "event_seq": 0,
            "last_event_seq": 0,
            "changed_files": [],
            "review_ready": False,
            "instruction": instruction,
            "acceptance": list(acceptance or []),
            "model": model,
            "reasoning_effort": reasoning_effort,
            "processed_rework_comment_ids": [],
            "runs": [],
            **metadata,
        }
        if isinstance(task_id, int):
            state["issue_number"] = int(task_id)
        validate_unicode_scalars(state)
        task_yaml_bytes = self._task_yaml(state).encode("utf-8", "strict")
        state_bytes = self._json_bytes(state)
        task_bytes = self._json_bytes(self._public_task_json(state))
        result_bytes = self._json_bytes({})
        self._create_task_atomic(
            key,
            {
                "task.yaml": task_yaml_bytes,
                "state.json": state_bytes,
                "task.json": task_bytes,
                "events.jsonl": b"",
                "stdout.log": b"",
                "stderr.log": b"",
                "result.json": result_bytes,
            },
        )
        return self.get_task(key)

    def initialize(self, task_id: Union[int, str], task_yaml: str, state: dict[str, Any]) -> None:
        """Create the V0.2 file set while preserving legacy callers."""
        key = self._task_key(task_id)
        now = utc_now()
        initial = {
            "task_id": key,
            "created_at": now,
            "updated_at": now,
            "processed_rework_comment_ids": [],
            "runs": [],
            "task_type": state.get("task_type", "code"),
            "project": state.get("project", ""),
            "thread_id": state.get("thread_id"),
            "turn_id": state.get("turn_id"),
            "worktree": state.get("worktree", state.get("worktree_path")),
            "worktree_path": state.get("worktree_path", state.get("worktree")),
            "events_file": "events.jsonl",
            "artifact_manifest": "result.json",
            "event_seq": int(state.get("event_seq", state.get("last_event_seq", 0))),
            "last_event_seq": int(state.get("last_event_seq", state.get("event_seq", 0))),
            **state,
        }
        if isinstance(task_id, int):
            initial["issue_number"] = int(task_id)
        validate_unicode_scalars(task_yaml)
        validate_unicode_scalars(initial)
        task_yaml_bytes = task_yaml.encode("utf-8", "strict")
        state_bytes = self._json_bytes(initial)
        task_bytes = self._json_bytes(self._public_task_json(initial))
        result_bytes = self._json_bytes({})
        self._create_task_atomic(
            key,
            {
                "task.yaml": task_yaml_bytes,
                "state.json": state_bytes,
                "task.json": task_bytes,
                "events.jsonl": b"",
                "stdout.log": b"",
                "stderr.log": b"",
                "result.json": result_bytes,
            },
        )

    def get_task(self, task_id: Union[int, str]) -> dict[str, Any]:
        key = self._task_key(task_id)
        task_path = self.task_path(key, "task.json")
        state_path = self.task_path(key, "state.json")
        if not task_path.is_file() and not state_path.is_file():
            raise ValueError(f"task does not exist: {key}")
        task = self._read_json(task_path) if task_path.is_file() else {}
        state = self._read_json(state_path) if state_path.is_file() else {}
        merged = dict(task)
        merged.update(state)
        merged.setdefault("task_id", key)
        merged.setdefault("events_file", "events.jsonl")
        merged.setdefault("artifact_manifest", "result.json")
        merged.setdefault("worktree", merged.get("worktree_path"))
        return merged

    def load_state(self, task_id: Union[int, str]) -> dict[str, Any]:
        return self.get_task(task_id)

    def update_task(self, task_id: Union[int, str], **updates: Any) -> dict[str, Any]:
        key = self._task_key(task_id)
        with self.task_lock(key):
            state = self.get_task(key)
            current = str(state.get("state", state.get("status", "")))
            requested_state = getattr(updates.get("state"), "value", updates.get("state"))
            requested_status = getattr(updates.get("status"), "value", updates.get("status"))
            if "state" in updates and str(requested_state) != current:
                raise ValueError("state changes require transition_task")
            if "status" in updates and str(requested_status) in TASK_TRANSITIONS and str(requested_status) != current:
                raise ValueError("lifecycle status changes require transition_task")
            state.update(updates)
            if "worktree_path" in updates and "worktree" not in updates:
                state["worktree"] = updates["worktree_path"]
            if "worktree" in updates and "worktree_path" not in updates:
                state["worktree_path"] = updates["worktree"]
            state["updated_at"] = utc_now()
            validate_unicode_scalars(state)
            self._write_compat_files(key, state, task_yaml=None)
            return self.get_task(key)

    def update_state(self, task_id: Union[int, str], **updates: Any) -> dict[str, Any]:
        return self.update_task(task_id, **updates)

    def transition_task(self, task_id: Union[int, str], from_state: str, to_state: str, reason: str) -> dict[str, Any]:
        """Compatibility transition helper for pre-runtime callers.

        New runtime code must enter lifecycle changes through
        ``TaskEventBus.transition``.  That boundary uses ``_apply_transition``
        below so the state, event cursor, and transition metadata are written
        as one snapshot update.
        """
        return self._apply_transition(task_id, from_state, to_state, reason)

    def _apply_transition(
        self,
        task_id: Union[int, str],
        from_state: str,
        to_state: str,
        reason: str,
        *,
        updates: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        key = self._task_key(task_id)
        validate_unicode_scalars(
            {
                "task_id": task_id,
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
                "updates": updates or {},
            }
        )
        with self.task_lock(key):
            state = self.get_task(key)
            from_state = getattr(from_state, "value", from_state)
            to_state = getattr(to_state, "value", to_state)
            current = str(state.get("state", state.get("status", "")))
            if current != str(from_state):
                raise ValueError(f"task state changed: expected {from_state}, found {current}")
            allowed = TASK_TRANSITIONS.get(current, set())
            if str(to_state) not in allowed:
                raise ValueError(f"invalid task transition: {current} -> {to_state}")
            extra = dict(updates or {})
            if {"state", "status"}.intersection(extra):
                raise ValueError("transition updates cannot replace lifecycle state")
            state.update(extra)
            state.update(
                {
                    "state": str(to_state),
                    "status": str(to_state),
                    "stage": str(reason),
                    "current_action": str(reason),
                    "last_transition_reason": str(reason),
                }
            )
            state["updated_at"] = utc_now()
            validate_unicode_scalars(state)
            self._write_compat_files(key, state, task_yaml=None)
            return self.get_task(key)

    def exists(self, task_id: Union[int, str]) -> bool:
        return self.task_path(task_id, "state.json").is_file() or self.task_path(task_id, "task.json").is_file()

    def is_incomplete_task(self, task_id: Union[int, str]) -> bool:
        """Read-only recognition of a legacy or otherwise incomplete task dir."""
        directory = self.task_dir(task_id)
        if not directory.is_dir() or directory.is_symlink():
            return False
        return not (
            self.task_path(task_id, "state.json").is_file()
            and self.task_path(task_id, "task.json").is_file()
        )

    def run_events_path(self, task_id: Union[int, str], run_number: int, run_kind: str) -> Path:
        validate_unicode_scalars({"task_id": task_id, "run_kind": run_kind})
        if int(run_number) <= 0:
            raise ValueError("run number must be positive")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", run_kind):
            raise ValueError("run kind must be a safe identifier")
        path = self.task_dir(task_id) / "runs" / f"{int(run_number):03d}-{run_kind}.events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return path

    def begin_run(self, task_id: Union[int, str], run_kind: str, *, requested_thread_id: Optional[str] = None) -> dict[str, Any]:
        key = self._task_key(task_id)
        state = self.get_task(key)
        runs = list(state.get("runs", []))
        run_number = max((int(item.get("number", 0)) for item in runs if isinstance(item, dict)), default=0) + 1
        record: dict[str, Any] = {
            "number": run_number,
            "kind": run_kind,
            "events_path": f"runs/{int(run_number):03d}-{run_kind}.events.jsonl",
            "requested_thread_id": requested_thread_id,
            "status": "running",
            "started_at": utc_now(),
        }
        next_state = dict(state)
        next_state.update({"runs": [*runs, record], "current_run": run_number})
        validate_unicode_scalars(
            {
                "task_id": key,
                "run_kind": run_kind,
                "requested_thread_id": requested_thread_id,
                "record": record,
                "state": next_state,
            }
        )
        self._preflight_compat_files(key, next_state, task_yaml=None)
        self.run_events_path(key, run_number, run_kind)
        runs.append(record)
        self.update_task(key, runs=runs, current_run=run_number)
        return record

    def finish_run(self, task_id: Union[int, str], run_number: int, **updates: Any) -> dict[str, Any]:
        key = self._task_key(task_id)
        state = self.get_task(key)
        runs = list(state.get("runs", []))
        for record in runs:
            if isinstance(record, dict) and int(record.get("number", 0)) == int(run_number):
                record.update(updates)
                record["finished_at"] = utc_now()
                break
        else:
            raise ValueError(f"run does not exist: {run_number}")
        validate_unicode_scalars({"run_number": run_number, "updates": updates, "runs": runs})
        self.update_task(key, runs=runs)
        return next(record for record in runs if int(record.get("number", 0)) == int(run_number))

    def write_result(self, task_id: Union[int, str], result: dict[str, Any]) -> None:
        validate_unicode_scalars(result)
        self._write_json(self.task_path(task_id, "result.json"), result)

    def save_artifacts(self, task_id: Union[int, str], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bounded = self._bounded_artifacts(artifacts)
        validate_unicode_scalars({"artifact_list": bounded, "artifact_manifest": "result.json"})
        self.write_result(task_id, {"artifact_list": bounded})
        self.update_task(task_id, artifact_manifest="result.json")
        return bounded

    @staticmethod
    def _bounded_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bounded: list[dict[str, Any]] = []
        for item in artifacts[:1000]:
            if not isinstance(item, dict):
                continue
            safe: dict[str, Any] = {}
            for key in ("path", "kind", "size", "bytes", "hash", "source"):
                value = item.get(key)
                if isinstance(value, (str, int, float, bool)):
                    safe[key] = value
            if isinstance(safe.get("path"), str) and not Path(safe["path"]).is_absolute() and ".." not in safe["path"].replace("\\", "/").split("/"):
                bounded.append(safe)
        return bounded

    def append_event(self, task_id: Union[int, str], event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        validate_unicode_scalars(event)
        with self.task_lock(task_id):
            event_record = dict(event)
            event_record.setdefault("time", utc_now())
            validate_unicode_scalars(event_record)
            event_bytes = (json.dumps(event_record, ensure_ascii=False) + "\n").encode("utf-8", "strict")
            self.task_path(task_id, "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
            with self.task_path(task_id, "events.jsonl").open("ab") as handle:
                handle.write(event_bytes)
                handle.flush()
                os.fsync(handle.fileno())

    def list_events(self, task_id: Union[int, str], *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(after_seq, bool) or int(after_seq) < 0:
            raise ValueError("after_seq must be non-negative")
        if isinstance(limit, bool) or int(limit) < 1 or int(limit) > 1000:
            raise ValueError("limit must be between 1 and 1000")
        path = self.task_path(task_id, "events.jsonl")
        if not path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            seq = event.get("seq")
            if seq is not None and int(seq) <= int(after_seq):
                continue
            result.append(event)
            if len(result) >= int(limit):
                break
        return result

    def read_events(self, task_id: Union[int, str], *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.list_events(task_id, after_seq=after_seq, limit=limit)

    def all_events(self, task_id: Union[int, str]) -> list[dict[str, Any]]:
        """Read the complete event journal for startup recovery."""
        path = self.task_path(task_id, "events.jsonl")
        if not path.is_file():
            return []
        result: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                result.append(event)
        return result

    def append_stdout(self, task_id: Union[int, str], text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("log text must be a string")
        validate_unicode_scalars(text)
        text.encode("utf-8", "strict")
        with self.task_path(task_id, "stdout.log").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def append_stderr(self, task_id: Union[int, str], text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("log text must be a string")
        validate_unicode_scalars(text)
        text.encode("utf-8", "strict")
        with self.task_path(task_id, "stderr.log").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def claim_new(self, task_id: Union[int, str]) -> bool:
        state = self.get_task(task_id)
        if state.get("status") != "ready":
            return False
        self.update_task(task_id, status="running", started_at=utc_now())
        return True

    def claim_rework_comment(self, task_id: Union[int, str], comment_id: str) -> bool:
        state = self.get_task(task_id)
        if state.get("status") != "review":
            return False
        processed = list(state.get("processed_rework_comment_ids", []))
        if comment_id in processed:
            return False
        processed.append(comment_id)
        self.update_task(task_id, status="running", processed_rework_comment_ids=processed)
        return True

    def reset_for_retry(self, task_id: Union[int, str]) -> dict[str, Any]:
        return self.update_task(task_id, status="ready", last_error=None)

    def list_task_ids(self) -> list[str]:
        tasks_root = self.root / "tasks"
        if not tasks_root.is_dir():
            return []
        return sorted(item.name for item in tasks_root.iterdir() if item.is_dir() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", item.name))

    def cleanup_stale_staging(self, *, max_age_seconds: float = 24 * 60 * 60) -> list[str]:
        """Remove only old, directly-owned staging entries when it is safe."""
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        tasks_root = self.root / "tasks"
        if not tasks_root.is_dir():
            return []
        try:
            entries = list(tasks_root.iterdir())
        except OSError as exc:
            logger.warning("stale task staging scan failed: %s", type(exc).__name__)
            return []

        now = time.time()
        removed: list[str] = []
        claims = [entry for entry in entries if self._CLAIM_NAME.fullmatch(entry.name) is not None]
        ordered_entries = [
            entry for entry in entries if self._STAGING_NAME.fullmatch(entry.name) is not None
        ] + claims
        for entry in ordered_entries:
            is_staging = self._STAGING_NAME.fullmatch(entry.name) is not None
            is_claim = self._CLAIM_NAME.fullmatch(entry.name) is not None
            if not is_staging and not is_claim:
                continue
            if self._is_reparse_point(entry):
                continue
            try:
                age = now - entry.stat().st_mtime
            except OSError:
                continue
            if age < max_age_seconds:
                continue
            if is_claim:
                pid = self._claim_pid(entry)
                if pid is None or self._pid_is_alive(pid) is not False:
                    continue
                try:
                    entry.unlink()
                except OSError as exc:
                    logger.warning("stale task claim cleanup failed: %s", type(exc).__name__)
                    continue
            else:
                claim = self._claim_for_staging(entry, claims)
                if claim is not None:
                    if self._is_reparse_point(claim):
                        continue
                    pid = self._claim_pid(claim)
                    if pid is None or self._pid_is_alive(pid) is not False:
                        continue
                if not entry.is_dir():
                    continue
                try:
                    shutil.rmtree(entry)
                except OSError as exc:
                    logger.warning("stale task staging cleanup failed: %s", type(exc).__name__)
                    continue
            removed.append(entry.name)
        return removed

    def next_execution_number(self) -> int:
        numbers = [int(item) for item in self.list_task_ids() if item.isdigit() and int(item) > 0]
        return max(numbers, default=0) + 1

    def _create_task_atomic(self, task_id: str, files: dict[str, bytes]) -> None:
        tasks_root = self.root / "tasks"
        final_directory = tasks_root / task_id
        claim_path = tasks_root / f".task-{task_id}.claim"
        staging: Optional[Path] = None
        claim_created = False
        try:
            tasks_root.mkdir(parents=True, exist_ok=True)
            if final_directory.exists() or final_directory.is_symlink():
                raise ValueError(f"task already exists: {task_id}")
            try:
                claim_fd = os.open(str(claim_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                raise ValueError(f"task creation already in progress: {task_id}") from exc
            claim_created = True
            with os.fdopen(claim_fd, "w", encoding="ascii", newline="\n") as handle:
                json.dump(
                    {"pid": os.getpid(), "created_at": utc_now(), "owner": uuid.uuid4().hex},
                    handle,
                    separators=(",", ":"),
                )
                handle.flush()
                os.fsync(handle.fileno())

            if final_directory.exists() or final_directory.is_symlink():
                raise ValueError(f"task already exists: {task_id}")
            self._cleanup_claim_owned_staging(task_id)
            staging = Path(tempfile.mkdtemp(prefix=f".task-{task_id}-", suffix=".tmp", dir=str(tasks_root)))
            for name in self._FILES:
                self._write_staged_file(staging / name, files[name])

            entries = list(staging.iterdir())
            expected = set(self._FILES)
            if len(entries) != len(expected) or {entry.name for entry in entries} != expected or any(not entry.is_file() for entry in entries):
                raise RuntimeError("task staging set is incomplete")
            try:
                # On Windows os.rename does not replace an existing directory.
                # The claim and the final existence checks close the cooperating
                # process race without using an overwrite-capable operation.
                os.rename(str(staging), str(final_directory))
            except OSError as exc:
                if final_directory.exists() or final_directory.is_symlink():
                    raise ValueError(f"task already exists: {task_id}") from exc
                raise
            staging = None
        finally:
            if staging is not None:
                self._cleanup_staging_path(staging)
            if claim_created:
                self._cleanup_claim_path(claim_path)

    def _cleanup_claim_owned_staging(self, task_id: str, *, max_age_seconds: float = 24 * 60 * 60) -> None:
        """Remove only old staging entries for a task whose claim we own."""
        if max_age_seconds < 0:
            raise ValueError("max_age_seconds must be non-negative")
        tasks_root = self.root / "tasks"
        prefix = f".task-{task_id}-"
        try:
            entries = list(tasks_root.iterdir())
        except OSError as exc:
            logger.warning("owned task staging scan failed: %s", type(exc).__name__)
            return

        now = time.time()
        for entry in entries:
            if not entry.name.startswith(prefix) or self._STAGING_NAME.fullmatch(entry.name) is None:
                continue
            if self._is_reparse_point(entry):
                continue
            try:
                if now - entry.stat().st_mtime < max_age_seconds or not entry.is_dir():
                    continue
                shutil.rmtree(entry)
            except OSError as exc:
                logger.warning("owned task staging cleanup failed: %s", type(exc).__name__)

    @staticmethod
    def _write_staged_file(path: Path, data: bytes) -> None:
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _cleanup_staging_path(path: Path) -> None:
        try:
            if path.exists() and not path.is_symlink():
                shutil.rmtree(path)
        except Exception as exc:
            logger.warning("task staging cleanup failed: %s", type(exc).__name__)

    @staticmethod
    def _cleanup_claim_path(path: Path) -> None:
        try:
            if path.exists() or path.is_symlink():
                path.unlink()
        except Exception as exc:
            logger.warning("task claim cleanup failed: %s", type(exc).__name__)

    @staticmethod
    def _is_reparse_point(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
            return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        except OSError:
            return True

    @staticmethod
    def _claim_pid(path: Path) -> Optional[int]:
        try:
            payload = json.loads(path.read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        pid = payload.get("pid") if isinstance(payload, dict) else None
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return None
        return pid

    @staticmethod
    def _claim_for_staging(staging: Path, claims: list[Path]) -> Optional[Path]:
        prefix_matches = [
            claim
            for claim in claims
            if staging.name.startswith(claim.name[: -len(".claim")] + "-")
        ]
        return max(prefix_matches, key=lambda path: len(path.name), default=None)

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return True

        if os.name != "nt":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            except Exception:
                return True
            return True

        process_query_limited_information = 0x1000
        error_invalid_parameter = 87
        still_active = 259
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return ctypes.get_last_error() != error_invalid_parameter
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return True
                return exit_code.value == still_active
            finally:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass
        except Exception:
            return True

    def _write_compat_files(self, task_id: str, state: dict[str, Any], *, task_yaml: Optional[str]) -> None:
        task_yaml_bytes, state_bytes, task_bytes, result_bytes = self._preflight_compat_files(
            task_id,
            state,
            task_yaml=task_yaml,
        )
        directory = self.task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        if task_yaml_bytes is not None:
            self.task_path(task_id, "task.yaml").write_bytes(task_yaml_bytes)
        self._write_json_bytes(self.task_path(task_id, "state.json"), state_bytes)
        self._write_json_bytes(self.task_path(task_id, "task.json"), task_bytes)
        for name in ("events.jsonl", "stdout.log", "stderr.log"):
            self.task_path(task_id, name).touch(exist_ok=True)
        if result_bytes is not None:
            self._write_json_bytes(self.task_path(task_id, "result.json"), result_bytes)

    def _preflight_compat_files(
        self,
        task_id: str,
        state: dict[str, Any],
        *,
        task_yaml: Optional[str],
    ) -> tuple[Optional[bytes], bytes, bytes, Optional[bytes]]:
        validate_unicode_scalars(state)
        state_bytes = self._json_bytes(state)
        task_bytes = self._json_bytes(self._public_task_json(state))

        task_yaml_bytes: Optional[bytes] = None
        yaml_path = self.task_path(task_id, "task.yaml")
        if task_yaml is not None:
            validate_unicode_scalars(task_yaml)
            task_yaml_bytes = task_yaml.encode("utf-8", "strict")
        elif not yaml_path.is_file():
            generated_yaml = self._task_yaml(state)
            validate_unicode_scalars(generated_yaml)
            task_yaml_bytes = generated_yaml.encode("utf-8", "strict")

        result_bytes = None
        result_path = self.task_path(task_id, "result.json")
        if not result_path.is_file():
            result_bytes = self._json_bytes({})
        return task_yaml_bytes, state_bytes, task_bytes, result_bytes

    @staticmethod
    def _public_task_json(state: dict[str, Any]) -> dict[str, Any]:
        return {
            "task_id": state.get("task_id"),
            "project": state.get("project", ""),
            "task_type": state.get("task_type", "code"),
            "state": state.get("state", state.get("status", TaskState.QUEUED.value)),
            "created_at": state.get("created_at", utc_now()),
            "updated_at": state.get("updated_at", utc_now()),
            "thread_id": state.get("thread_id"),
            "turn_id": state.get("turn_id"),
            "worktree": state.get("worktree", state.get("worktree_path")),
            "events_file": state.get("events_file", "events.jsonl"),
            "artifact_manifest": state.get("artifact_manifest", "result.json"),
            "stage": state.get("stage", state.get("current_action", "")),
            "changed_files": list(state.get("changed_files", [])),
            "last_event_seq": int(state.get("last_event_seq", state.get("event_seq", 0))),
            "instruction": state.get("instruction", ""),
            "acceptance": list(state.get("acceptance", [])),
            "model": state.get("model"),
            "reasoning_effort": state.get("reasoning_effort"),
        }

    @staticmethod
    def _task_yaml(state: dict[str, Any]) -> str:
        return json.dumps(
            {
                "version": 1,
                "task_type": state.get("task_type", "code"),
                "project": state.get("project", ""),
                "title": str(state.get("instruction", ""))[:200],
                "acceptance": state.get("acceptance", []),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"task snapshot must be an object: {path.name}")
        return value

    @staticmethod
    def _json_bytes(value: dict[str, Any]) -> bytes:
        validate_unicode_scalars(value)
        return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8", "strict")

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        data = TaskStore._json_bytes(value)
        TaskStore._write_json_bytes(path, data)

    @staticmethod
    def _write_json_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
