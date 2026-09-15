from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from .models import TASK_TRANSITIONS, TaskState


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Filesystem-backed task snapshots, events, logs, and manifests."""

    _FILES = ("task.json", "task.yaml", "state.json", "events.jsonl", "stdout.log", "stderr.log", "result.json")

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
        if self.exists(key):
            raise ValueError(f"task already exists: {key}")
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
        self._write_compat_files(key, state, task_yaml=self._task_yaml(state))
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
        self._write_compat_files(key, initial, task_yaml=task_yaml)

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
        state = self.get_task(key)
        state.update(updates)
        if "worktree_path" in updates and "worktree" not in updates:
            state["worktree"] = updates["worktree_path"]
        if "worktree" in updates and "worktree_path" not in updates:
            state["worktree_path"] = updates["worktree"]
        state["updated_at"] = utc_now()
        self._write_compat_files(key, state, task_yaml=None)
        return self.get_task(key)

    def update_state(self, task_id: Union[int, str], **updates: Any) -> dict[str, Any]:
        return self.update_task(task_id, **updates)

    def transition_task(self, task_id: Union[int, str], from_state: str, to_state: str, reason: str) -> dict[str, Any]:
        key = self._task_key(task_id)
        state = self.get_task(key)
        current = str(state.get("state", state.get("status", "")))
        if current != str(from_state):
            raise ValueError(f"task state changed: expected {from_state}, found {current}")
        allowed = TASK_TRANSITIONS.get(current, set())
        if str(to_state) not in allowed:
            raise ValueError(f"invalid task transition: {current} -> {to_state}")
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
        self._write_compat_files(key, state, task_yaml=None)
        return self.get_task(key)

    def exists(self, task_id: Union[int, str]) -> bool:
        return self.task_path(task_id, "state.json").is_file() or self.task_path(task_id, "task.json").is_file()

    def run_events_path(self, task_id: Union[int, str], run_number: int, run_kind: str) -> Path:
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
        events_path = self.run_events_path(key, run_number, run_kind)
        record: dict[str, Any] = {
            "number": run_number,
            "kind": run_kind,
            "events_path": events_path.relative_to(self.task_dir(key)).as_posix(),
            "requested_thread_id": requested_thread_id,
            "status": "running",
            "started_at": utc_now(),
        }
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
        self.update_task(key, runs=runs)
        return next(record for record in runs if int(record.get("number", 0)) == int(run_number))

    def write_result(self, task_id: Union[int, str], result: dict[str, Any]) -> None:
        self._write_json(self.task_path(task_id, "result.json"), result)

    def save_artifacts(self, task_id: Union[int, str], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        self.write_result(task_id, {"artifact_list": bounded})
        self.update_task(task_id, artifact_manifest="result.json")
        return bounded

    def append_event(self, task_id: Union[int, str], event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            raise ValueError("event must be an object")
        event_record = dict(event)
        event_record.setdefault("time", utc_now())
        self.task_path(task_id, "events.jsonl").parent.mkdir(parents=True, exist_ok=True)
        with self.task_path(task_id, "events.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event_record, ensure_ascii=False) + "\n")
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

    def append_stdout(self, task_id: Union[int, str], text: str) -> None:
        with self.task_path(task_id, "stdout.log").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def append_stderr(self, task_id: Union[int, str], text: str) -> None:
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

    def next_execution_number(self) -> int:
        numbers = [int(item) for item in self.list_task_ids() if item.isdigit() and int(item) > 0]
        return max(numbers, default=0) + 1

    def _write_compat_files(self, task_id: str, state: dict[str, Any], *, task_yaml: Optional[str]) -> None:
        directory = self.task_dir(task_id)
        directory.mkdir(parents=True, exist_ok=True)
        if task_yaml is not None:
            self.task_path(task_id, "task.yaml").write_text(task_yaml, encoding="utf-8")
        elif not self.task_path(task_id, "task.yaml").is_file():
            self.task_path(task_id, "task.yaml").write_text(self._task_yaml(state), encoding="utf-8")
        self._write_json(self.task_path(task_id, "state.json"), state)
        self._write_json(self.task_path(task_id, "task.json"), self._public_task_json(state))
        for name in ("events.jsonl", "stdout.log", "stderr.log"):
            self.task_path(task_id, name).touch(exist_ok=True)
        if not self.task_path(task_id, "result.json").is_file():
            self._write_json(self.task_path(task_id, "result.json"), {})

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
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

