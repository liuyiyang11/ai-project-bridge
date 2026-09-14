from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from ..github import find_executable


class CodexUnavailableError(RuntimeError):
    """Raised when the configured Codex binary is unavailable."""


class CodexResumeMismatchError(RuntimeError):
    """Raised when a resumed Codex session returns a different thread."""

    def __init__(self, requested_thread_id: str, returned_thread_id: Optional[str]):
        self.requested_thread_id = requested_thread_id
        self.returned_thread_id = returned_thread_id
        returned = returned_thread_id or "<missing>"
        super().__init__(f"Codex resume thread mismatch: requested {requested_thread_id!r}, returned {returned!r}")


@dataclass(frozen=True)
class CodexResult:
    task_id: str
    thread_id: Optional[str]
    exit_code: int
    final_message: str
    events_path: Path
    stdout: str = ""
    stderr: str = ""
    requested_thread_id: Optional[str] = None


class CodexRunner:
    def __init__(self, binary: str = "codex", popen: Optional[Callable[..., Any]] = None, available: Optional[bool] = None):
        self.binary = binary
        resolved = find_executable(binary)
        # A production runner only executes a path that was resolved at startup.
        # `available=True` remains a narrow test seam for fake subprocess tests.
        self.executable = resolved or (binary if available is True else None)
        self._popen = popen or subprocess.Popen
        self._available = self.executable is not None if available is None else available

    def start_task(self, prompt: str, cwd: Path, events_path: Path, *, final_file: Optional[Path] = None) -> CodexResult:
        command = [
            self.executable or self.binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--approve-for-me",
            "--cd",
            str(Path(cwd).resolve()),
            "-o",
            str(final_file or events_path.with_name("codex-final.txt")),
            "-",
        ]
        return self._run(command, prompt, cwd, events_path, final_file)

    def resume_task(self, thread_id: str, prompt: str, cwd: Path, events_path: Path, *, final_file: Optional[Path] = None) -> CodexResult:
        command = [
            self.executable or self.binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--approve-for-me",
            "--cd",
            str(Path(cwd).resolve()),
            "resume",
            thread_id,
            "-o",
            str(final_file or events_path.with_name("codex-final.txt")),
            "-",
        ]
        return self._run(command, prompt, cwd, events_path, final_file, requested_thread_id=thread_id)

    def _run(
        self,
        command: list[str],
        prompt: str,
        cwd: Path,
        events_path: Path,
        final_file: Optional[Path],
        *,
        requested_thread_id: Optional[str] = None,
    ) -> CodexResult:
        if not self._available:
            raise CodexUnavailableError("Codex CLI is not available; install it or update codex_binary")
        events_path.parent.mkdir(parents=True, exist_ok=True)
        final_path = final_file or events_path.with_name("codex-final.txt")
        try:
            process = self._popen(
                command,
                cwd=str(Path(cwd).resolve()),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            stdout, stderr = process.communicate(prompt, timeout=86400)
            exit_code = int(process.returncode)
        except FileNotFoundError as exc:
            raise CodexUnavailableError("Codex CLI is not available; install it or update codex_binary") from exc
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            exit_code = 124

        # Each caller normally supplies a per-run path.  Append here as a second
        # guard so a direct runner reuse can never erase an earlier event stream.
        with events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(stdout or "")
        events = self._parse_events(stdout or "")
        thread_id = self._find_thread_id(events)
        if requested_thread_id is not None and thread_id != requested_thread_id:
            raise CodexResumeMismatchError(requested_thread_id, thread_id)
        final_message = final_path.read_text(encoding="utf-8") if final_path.is_file() else self._find_final_message(events)
        return CodexResult(
            str(uuid.uuid4()),
            thread_id,
            exit_code,
            final_message.strip(),
            events_path,
            stdout or "",
            stderr or "",
            requested_thread_id,
        )

    @staticmethod
    def _parse_events(stdout: str) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for line in stdout.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                parsed.append(value)
        return parsed

    @classmethod
    def _find_thread_id(cls, events: list[dict[str, Any]]) -> Optional[str]:
        for event in events:
            event_type = event.get("type") or event.get("event")
            if event_type != "thread.started":
                continue
            found = cls._find_value(event, {"thread_id", "threadId", "session_id", "sessionId"})
            if found:
                return str(found)
        return None

    @classmethod
    def _find_final_message(cls, events: list[dict[str, Any]]) -> str:
        for event in reversed(events):
            found = cls._find_value(event, {"final_message", "finalMessage", "message", "text"})
            if isinstance(found, str) and found.strip():
                return found
        return ""

    @classmethod
    def _find_value(cls, value: Any, keys: set[str]) -> Any:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in keys and item:
                    return item
                found = cls._find_value(item, keys)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = cls._find_value(item, keys)
                if found:
                    return found
        return None
