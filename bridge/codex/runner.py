from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..github import find_executable


class CodexUnavailableError(RuntimeError):
    """Raised when the configured Codex binary is unavailable."""


@dataclass(frozen=True)
class CodexResult:
    task_id: str
    thread_id: str | None
    exit_code: int
    final_message: str
    events_path: Path
    stdout: str = ""
    stderr: str = ""


class CodexRunner:
    def __init__(self, binary: str = "codex", popen: Callable[..., Any] | None = None, available: bool | None = None):
        self.binary = binary
        self._popen = popen or subprocess.Popen
        self._available = find_executable(binary) is not None if available is None else available

    def start_task(self, prompt: str, cwd: Path, events_path: Path, *, final_file: Path | None = None) -> CodexResult:
        command = [
            self.binary,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write",
            "--ask-for-approval",
            "never",
            "--cd",
            str(Path(cwd).resolve()),
            "-o",
            str(final_file or events_path.with_name("codex-final.txt")),
            "-",
        ]
        return self._run(command, prompt, cwd, events_path, final_file)

    def resume_task(self, thread_id: str, prompt: str, cwd: Path, events_path: Path, *, final_file: Path | None = None) -> CodexResult:
        command = [
            self.binary,
            "exec",
            "resume",
            thread_id,
            "--json",
            "-o",
            str(final_file or events_path.with_name("codex-final.txt")),
            "-",
        ]
        return self._run(command, prompt, cwd, events_path, final_file)

    def _run(self, command: list[str], prompt: str, cwd: Path, events_path: Path, final_file: Path | None) -> CodexResult:
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

        events_path.write_text(stdout or "", encoding="utf-8")
        events = self._parse_events(stdout or "")
        thread_id = self._find_thread_id(events)
        final_message = final_path.read_text(encoding="utf-8") if final_path.is_file() else self._find_final_message(events)
        return CodexResult(str(uuid.uuid4()), thread_id, exit_code, final_message.strip(), events_path, stdout or "", stderr or "")

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
    def _find_thread_id(cls, events: list[dict[str, Any]]) -> str | None:
        for event in events:
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
