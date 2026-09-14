from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TaskStore:
    """Filesystem-backed, append-friendly state for one Bridge control repo."""

    _FILES = ("task.yaml", "state.json", "events.jsonl", "stdout.log", "stderr.log", "result.json")

    def __init__(self, root: Path):
        self.root = Path(root)

    def task_dir(self, issue_number: int) -> Path:
        if int(issue_number) <= 0:
            raise ValueError("issue number must be positive")
        return self.root / "tasks" / str(int(issue_number))

    def task_path(self, issue_number: int, name: str) -> Path:
        if name not in self._FILES:
            raise ValueError(f"unsupported task file: {name}")
        return self.task_dir(issue_number) / name

    def run_events_path(self, issue_number: int, run_number: int, run_kind: str) -> Path:
        """Return the append-only JSONL path for one Codex run."""
        if int(run_number) <= 0:
            raise ValueError("run number must be positive")
        if not re.fullmatch(r"[a-z][a-z0-9-]*", run_kind):
            raise ValueError("run kind must be a safe identifier")
        path = self.task_dir(issue_number) / "runs" / f"{int(run_number):03d}-{run_kind}.events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        return path

    def initialize(self, issue_number: int, task_yaml: str, state: dict[str, Any]) -> None:
        directory = self.task_dir(issue_number)
        directory.mkdir(parents=True, exist_ok=True)
        self.task_path(issue_number, "task.yaml").write_text(task_yaml, encoding="utf-8")
        initial = {
            "issue_number": int(issue_number),
            "created_at": utc_now(),
            "processed_rework_comment_ids": [],
            "runs": [],
            **state,
        }
        self._write_json(self.task_path(issue_number, "state.json"), initial)
        for name in ("events.jsonl", "stdout.log", "stderr.log"):
            self.task_path(issue_number, name).touch(exist_ok=True)
        self._write_json(self.task_path(issue_number, "result.json"), {})

    def exists(self, issue_number: int) -> bool:
        return self.task_path(issue_number, "state.json").is_file()

    def load_state(self, issue_number: int) -> dict[str, Any]:
        path = self.task_path(issue_number, "state.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def update_state(self, issue_number: int, **updates: Any) -> dict[str, Any]:
        state = self.load_state(issue_number)
        state.update(updates)
        state["updated_at"] = utc_now()
        self._write_json(self.task_path(issue_number, "state.json"), state)
        return state

    def begin_run(self, issue_number: int, run_kind: str, *, requested_thread_id: Optional[str] = None) -> dict[str, Any]:
        state = self.load_state(issue_number)
        runs = list(state.get("runs", []))
        run_number = max((int(item.get("number", 0)) for item in runs if isinstance(item, dict)), default=0) + 1
        events_path = self.run_events_path(issue_number, run_number, run_kind)
        record: dict[str, Any] = {
            "number": run_number,
            "kind": run_kind,
            "events_path": events_path.relative_to(self.task_dir(issue_number)).as_posix(),
            "requested_thread_id": requested_thread_id,
            "status": "running",
            "started_at": utc_now(),
        }
        runs.append(record)
        state.update({"runs": runs, "current_run": run_number, "updated_at": utc_now()})
        self._write_json(self.task_path(issue_number, "state.json"), state)
        return record

    def finish_run(self, issue_number: int, run_number: int, **updates: Any) -> dict[str, Any]:
        state = self.load_state(issue_number)
        runs = list(state.get("runs", []))
        for record in runs:
            if isinstance(record, dict) and int(record.get("number", 0)) == int(run_number):
                record.update(updates)
                record["finished_at"] = utc_now()
                break
        else:
            raise ValueError(f"run does not exist: {run_number}")
        state.update({"runs": runs, "updated_at": utc_now()})
        self._write_json(self.task_path(issue_number, "state.json"), state)
        return next(record for record in runs if int(record.get("number", 0)) == int(run_number))

    def write_result(self, issue_number: int, result: dict[str, Any]) -> None:
        self._write_json(self.task_path(issue_number, "result.json"), result)

    def append_event(self, issue_number: int, event: dict[str, Any]) -> None:
        event_record = {"timestamp": utc_now(), **event}
        with self.task_path(issue_number, "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_record, ensure_ascii=False) + "\n")

    def append_stdout(self, issue_number: int, text: str) -> None:
        with self.task_path(issue_number, "stdout.log").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def append_stderr(self, issue_number: int, text: str) -> None:
        with self.task_path(issue_number, "stderr.log").open("a", encoding="utf-8") as handle:
            handle.write(text)

    def claim_new(self, issue_number: int) -> bool:
        state = self.load_state(issue_number)
        if state.get("status") != "ready":
            return False
        state.update({"status": "running", "started_at": utc_now(), "updated_at": utc_now()})
        self._write_json(self.task_path(issue_number, "state.json"), state)
        return True

    def claim_rework_comment(self, issue_number: int, comment_id: str) -> bool:
        state = self.load_state(issue_number)
        if state.get("status") != "review":
            return False
        processed = list(state.get("processed_rework_comment_ids", []))
        if comment_id in processed:
            return False
        processed.append(comment_id)
        state.update({"status": "running", "processed_rework_comment_ids": processed, "updated_at": utc_now()})
        self._write_json(self.task_path(issue_number, "state.json"), state)
        return True

    def reset_for_retry(self, issue_number: int) -> dict[str, Any]:
        return self.update_state(issue_number, status="ready", last_error=None)

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

