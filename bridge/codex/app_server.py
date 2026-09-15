from __future__ import annotations

import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from ..github import find_executable
from .protocol import (
    CodexProcessError,
    CodexProtocolError,
    CodexProtocolTimeout,
    CodexRequestError,
    JsonRpcMessage,
    notification_payload,
    parse_message,
    request_payload,
    safe_notification,
)


logger = logging.getLogger(__name__)


class CodexAppServerClient:
    """Synchronous JSON-RPC client over ``codex app-server --stdio``.

    The stdout reader is isolated in a small daemon thread so notifications can
    stream while a request is pending.  The client never invokes a shell and
    never reads Codex configuration or authentication files itself; the
    bundled executable is resolved using the same existing discovery seam as
    the V0.1 runner.
    """

    def __init__(
        self,
        binary: str = "codex",
        cwd: Optional[Path] = None,
        *,
        worktree: Optional[Path] = None,
        popen: Optional[Callable[..., Any]] = None,
        request_timeout: float = 30.0,
        initialize_timeout: Optional[float] = None,
        notification_handler: Optional[Callable[[dict[str, Any]], None]] = None,
        process_error_handler: Optional[Callable[[Exception], None]] = None,
        available: Optional[bool] = None,
    ):
        self.binary = binary
        resolved = find_executable(binary)
        # `shutil.which` may return the npm .cmd/.ps1 shim on Windows.  A
        # shell-less app-server must use the bundled native executable when it
        # is available, while retaining the explicit test seam.
        if resolved and Path(resolved).suffix.lower() in {".cmd", ".ps1", ".bat"}:
            native = find_executable("codex.exe")
            resolved = native or resolved
        self.executable = resolved or (binary if available is True else None)
        self._available = self.executable is not None if available is None else available
        self.cwd = Path(worktree or cwd or Path.cwd()).resolve()
        self._popen = popen or subprocess.Popen
        self.request_timeout = float(request_timeout)
        self.initialize_timeout = float(initialize_timeout or request_timeout)
        self.notification_handler = notification_handler
        self.process_error_handler = process_error_handler
        self.process: Any = None
        self._next_id = 1
        self._inbound: queue.Queue[Any] = queue.Queue()
        self._pending: dict[Any, dict[str, Any]] = {}
        self._pending_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._started = False
        self._closed = False
        self.stderr = ""

    @property
    def command(self) -> list[str]:
        return [self.executable or self.binary, "app-server", "--stdio"]

    def start(self) -> dict[str, Any]:
        if self._started:
            raise CodexProtocolError("Codex app-server client is already started")
        if not self._available:
            raise CodexProcessError("Codex CLI is not available; install it or update codex_binary")
        if not self.cwd.is_dir():
            raise CodexProtocolError(f"Codex app-server cwd does not exist: {self.cwd}")
        self._closed = False
        try:
            self.process = self._popen(
                self.command,
                cwd=str(self.cwd),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
            )
            logger.info(
                "Codex app-server process start executable=%s cwd=%s",
                self.executable or self.binary,
                self.cwd,
            )
        except FileNotFoundError as exc:
            raise CodexProcessError("Codex CLI is not available; install it or update codex_binary") from exc
        self._started = True
        self._reader = threading.Thread(target=self._read_stdout, name="bridge-codex-app-server", daemon=True)
        self._reader.start()
        threading.Thread(target=self._read_stderr, name="bridge-codex-app-server-stderr", daemon=True).start()
        return self.initialize(timeout=self.initialize_timeout)

    def initialize(self, *, timeout: Optional[float] = None) -> dict[str, Any]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {"name": "ai-project-bridge", "version": "0.2.0"},
                "capabilities": {"experimentalApi": False},
            },
            timeout=timeout,
        )
        logger.info("Codex app-server initialize response received fields=%s", sorted(result))
        self.notify("initialized")
        return result

    def model_list(self, *, cursor: Optional[str] = None, limit: Optional[int] = None, include_hidden: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"includeHidden": include_hidden}
        if cursor:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = int(limit)
        return self.request("model/list", params)

    def list_models(self, **kwargs: Any) -> dict[str, Any]:
        return self.model_list(**kwargs)

    def thread_start(
        self,
        *,
        model: Optional[str] = None,
        cwd: Optional[Path] = None,
        ephemeral: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "cwd": str(Path(cwd or self.cwd).resolve()),
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
            "ephemeral": ephemeral,
        }
        if model:
            params["model"] = model
        result = self.request("thread/start", params)
        logger.info("Codex app-server thread/start response received fields=%s", sorted(result))
        return result

    def thread_resume(
        self,
        thread_id: str,
        *,
        model: Optional[str] = None,
        cwd: Optional[Path] = None,
    ) -> dict[str, Any]:
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CodexProtocolError("thread_id must be non-empty")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "cwd": str(Path(cwd or self.cwd).resolve()),
            "sandbox": "workspace-write",
            "approvalPolicy": "never",
        }
        if model:
            params["model"] = model
        return self.request("thread/resume", params)

    def start_thread(self, **kwargs: Any) -> dict[str, Any]:
        return self.thread_start(**kwargs)

    def resume_thread(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.thread_resume(thread_id, **kwargs)

    def turn_start(
        self,
        thread_id: str,
        instruction: str,
        *,
        model: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
    ) -> dict[str, Any]:
        if not isinstance(instruction, str) or not instruction.strip():
            raise CodexProtocolError("turn instruction must be non-empty")
        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": instruction}],
        }
        if model:
            params["model"] = model
        # Current app-server v2 calls this field `effort`; the Bridge's public
        # API keeps the clearer `reasoning_effort` name.
        if reasoning_effort:
            params["effort"] = reasoning_effort
        logger.info(
            "Codex app-server turn/start request thread_id=%s instruction_chars=%d model=%s",
            thread_id,
            len(instruction),
            model or "default",
        )
        result = self.request("turn/start", params)
        logger.info("Codex app-server turn/start response received fields=%s", sorted(result))
        return result

    def start_turn(self, thread_id: str, instruction: str, **kwargs: Any) -> dict[str, Any]:
        return self.turn_start(thread_id, instruction, **kwargs)

    def turn_steer(self, thread_id: str, turn_id: str, instruction: str) -> dict[str, Any]:
        if not isinstance(instruction, str) or not instruction.strip():
            raise CodexProtocolError("steer instruction must be non-empty")
        return self.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": turn_id,
                "input": [{"type": "text", "text": instruction}],
            },
        )

    def steer_turn(self, thread_id: str, turn_id: str, instruction: str) -> dict[str, Any]:
        return self.turn_steer(thread_id, turn_id, instruction)

    def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})

    def interrupt_turn(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        return self.turn_interrupt(thread_id, turn_id)

    def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        self._write(notification_payload(method, params))

    def request(self, method: str, params: Optional[dict[str, Any]] = None, *, timeout: Optional[float] = None) -> dict[str, Any]:
        if not self._started or self._closed:
            raise CodexProtocolError("Codex app-server client is not running")
        with self._pending_lock:
            message_id = self._next_id
            self._next_id += 1
        self._write(request_payload(message_id, method, params))
        deadline = time.monotonic() + float(self.request_timeout if timeout is None else timeout)
        while True:
            with self._pending_lock:
                pending = self._pending.pop(message_id, None)
            if pending is not None:
                message = JsonRpcMessage(pending)
            else:
                remaining = max(0.0, deadline - time.monotonic())
                if remaining == 0:
                    raise CodexProtocolTimeout(f"timed out waiting for app-server response to {method}")
                try:
                    # A SessionManager wait loop may pump notifications in a
                    # second thread and place this request's response in
                    # ``_pending``. Polling the inbound queue bounds how long
                    # this request waits before checking that handoff.
                    item = self._get_inbound(min(0.05, remaining))
                except CodexProtocolTimeout:
                    continue
                if item is None:
                    raise CodexProcessError("Codex app-server exited while handling request", returncode=self._returncode(), stderr=self.stderr)
                message = item
            if message.is_response:
                if message.message_id != message_id:
                    if message.message_id is not None:
                        with self._pending_lock:
                            self._pending[message.message_id] = message.value
                    continue
                if "error" in message.value:
                    error = message.value.get("error") or {}
                    if isinstance(error, dict):
                        raise CodexRequestError(error.get("code"), str(error.get("message", "request failed")), error.get("data"))
                    raise CodexRequestError(None, str(error))
                result = message.value.get("result", {})
                return result if isinstance(result, dict) else {"value": result}
            self._handle_notification(message)

    def drain_notifications(self, *, limit: int = 100) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for _ in range(max(0, limit)):
            try:
                item = self._inbound.get_nowait()
            except queue.Empty:
                break
            if item is None:
                # Preserve the reader's EOF sentinel for an in-flight request.
                # Otherwise a concurrent notification pump could consume it and
                # make that request wait until its timeout instead of reporting
                # the process exit promptly.
                self._inbound.put(None)
                break
            if item.is_response:
                if item.message_id is not None:
                    with self._pending_lock:
                        self._pending[item.message_id] = item.value
                continue
            safe = safe_notification(item)
            if safe:
                result.append(safe)
            self._handle_notification(item)
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is None:
            return
        try:
            if getattr(process, "stdin", None) is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            if self._returncode() is None and hasattr(process, "terminate"):
                process.terminate()
        except OSError:
            pass
        try:
            if hasattr(process, "wait"):
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if hasattr(process, "kill"):
                    process.kill()
            except OSError:
                pass
        logger.info("Codex app-server process exit code=%s", self._returncode())

    def __enter__(self) -> "CodexAppServerClient":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _write(self, value: dict[str, Any]) -> None:
        process = self.process
        if process is None or getattr(process, "stdin", None) is None:
            raise CodexProtocolError("Codex app-server stdin is unavailable")
        line = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            with self._write_lock:
                process.stdin.write(line)
                process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexProcessError("failed to write to Codex app-server", returncode=self._returncode(), stderr=self.stderr) from exc

    def _read_stdout(self) -> None:
        stream = getattr(self.process, "stdout", None)
        if stream is None:
            self._inbound.put(None)
            return
        try:
            while True:
                line = stream.readline()
                if line in ("", b""):
                    break
                if isinstance(line, bytes):
                    line = line.decode("utf-8", errors="replace")
                try:
                    message = parse_message(line)
                    if message.is_response:
                        logger.info("Codex app-server response received id=%s", message.message_id)
                    elif message.method:
                        logger.info("Codex app-server notification receive method=%s", message.method)
                    self._inbound.put(message)
                except CodexProtocolError as exc:
                    self._inbound.put(JsonRpcMessage({"method": "error", "params": {"message": str(exc)}}))
        finally:
            self._inbound.put(None)
            logger.warning("Codex app-server stdout EOF returncode=%s", self._returncode())
            if not self._closed and self.process_error_handler:
                try:
                    self.process_error_handler(CodexProcessError("Codex app-server stdout closed", returncode=self._returncode(), stderr=self.stderr))
                except Exception:
                    pass

    def _read_stderr(self) -> None:
        stream = getattr(self.process, "stderr", None)
        if stream is None:
            return
        try:
            value = stream.read()
            if value:
                text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
                self.stderr += text
                logger.info("Codex app-server stderr received chars=%d", len(text))
        except (OSError, ValueError):
            return

    def _get_inbound(self, timeout: float) -> Optional[JsonRpcMessage]:
        try:
            return self._inbound.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexProtocolTimeout("timed out waiting for Codex app-server output") from exc

    def _handle_notification(self, message: JsonRpcMessage) -> None:
        safe = safe_notification(message)
        if safe:
            self._call_notification_handler(safe)
        # A server request (for example an approval request) must not be
        # silently approved.  The configured thread uses approvalPolicy=never;
        # if a request still arrives, deny it explicitly.
        if message.method and "id" in message.value and not message.is_response:
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message.message_id,
                    "error": {"code": -32601, "message": "Bridge does not handle app-server approval requests"},
                }
            )

    def _call_notification_handler(self, event: dict[str, Any]) -> None:
        if self.notification_handler:
            try:
                self.notification_handler(event)
            except Exception:
                # A progress observer must never kill the protocol reader.
                pass

    def _returncode(self) -> Optional[int]:
        process = self.process
        if process is None or not hasattr(process, "poll"):
            return getattr(process, "returncode", None) if process is not None else None
        return process.poll()
