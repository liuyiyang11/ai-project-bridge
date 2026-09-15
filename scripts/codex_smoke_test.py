"""Manual-only smoke test for a real Codex app-server process.

Run from the repository root with:

    python scripts/codex_smoke_test.py

The Codex workspace is always a temporary directory. This script never opens
the real project, invokes Git, or reads authentication/configuration files.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from bridge.codex.app_server import CodexAppServerClient
from bridge.github import find_executable


INSTRUCTION = "Create hello.py with a hello function and add a small test."


def _identifier(value: Any, key: str) -> Optional[str]:
    if not isinstance(value, dict):
        return None
    nested = value.get(key)
    if isinstance(nested, dict) and isinstance(nested.get("id"), str):
        return nested["id"]
    for candidate in ("threadId", "thread_id", "turnId", "turn_id", "id"):
        if isinstance(value.get(candidate), str) and value[candidate]:
            return value[candidate]
    return None


def _new_files(root: Path, before: set[str]) -> list[str]:
    current: set[str] = set()
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.parts:
            current.add(path.relative_to(root).as_posix())
    return sorted(current - before)


def run() -> int:
    executable = find_executable("codex")
    if not executable:
        print(json.dumps({"error": "codex executable was not found"}, ensure_ascii=False))
        return 2

    with tempfile.TemporaryDirectory(prefix="bridge-codex-smoke-") as directory:
        worktree = Path(directory).resolve()
        before = _new_files(worktree, set())
        events: list[dict[str, Any]] = []

        def on_event(event: dict[str, Any]) -> None:
            if len(events) < 1000:
                events.append(event)

        client = CodexAppServerClient(executable, cwd=worktree, notification_handler=on_event)
        try:
            initialize = client.start()
            catalog = client.model_list()
            thread_response = client.thread_start(cwd=worktree)
            thread_id = _identifier(thread_response.get("thread") if isinstance(thread_response, dict) else None, "thread") or _identifier(thread_response, "thread")
            if not thread_id:
                raise RuntimeError("thread/start returned no thread id")
            turn_response = client.turn_start(thread_id, INSTRUCTION)
            turn_id = _identifier(turn_response.get("turn") if isinstance(turn_response, dict) else None, "turn") or _identifier(turn_response, "turn")
            deadline = time.monotonic() + 300.0
            completed = False
            while time.monotonic() < deadline:
                client.drain_notifications(limit=100)
                if any(item.get("method") == "turn/completed" for item in events):
                    completed = True
                    break
                time.sleep(0.1)
            summary = {
                "thread_id": thread_id,
                "turn_id": turn_id,
                "completed": completed,
                "event_count": len(events),
                "events": events,
                "changed_files": _new_files(worktree, set(before)),
                "model_count": len(catalog.get("data", [])) if isinstance(catalog, dict) and isinstance(catalog.get("data"), list) else None,
                "server": initialize.get("serverInfo") if isinstance(initialize, dict) else None,
                "workspace": "temporary-directory",
            }
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0 if completed else 1
        except Exception as exc:
            print(json.dumps({"error": f"{type(exc).__name__}: {exc}", "workspace": "temporary-directory"}, ensure_ascii=False))
            return 1
        finally:
            client.close()


if __name__ == "__main__":
    raise SystemExit(run())
