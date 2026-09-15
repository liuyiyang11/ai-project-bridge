from __future__ import annotations

from typing import Any, Callable, Optional


class FakeAppServer:
    """Deterministic in-process app-server double for Bridge tests.

    It exposes the same small client surface used by ``CodexSessionManager``
    and records every protocol operation.  Tests can set ``auto_complete`` to
    false and call ``emit`` to exercise streaming and control transitions.
    """

    def __init__(
        self,
        *,
        models: Optional[list[dict[str, Any]]] = None,
        thread_id: str = "fake-thread",
        turn_id: str = "fake-turn-1",
        auto_complete: bool = True,
        final_message: str = "Fake app-server completed.",
        notification_handler: Optional[Callable[[dict[str, Any]], None]] = None,
        process_error_handler: Optional[Callable[[Exception], None]] = None,
        **_: Any,
    ):
        self.models = models or [
            {
                "id": "fake-model",
                "model": "fake-model",
                "displayName": "Fake model",
                "description": "Test model",
                "isDefault": True,
                "hidden": False,
                "defaultReasoningEffort": "medium",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "low"},
                    {"reasoningEffort": "medium", "description": "medium"},
                    {"reasoningEffort": "high", "description": "high"},
                ],
            }
        ]
        self.thread_id = thread_id
        self.next_turn_number = 1
        self.turn_id = turn_id
        self.auto_complete = auto_complete
        self.final_message = final_message
        self.notification_handler = notification_handler
        self.process_error_handler = process_error_handler
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.notifications: list[dict[str, Any]] = []
        self.started = False
        self.closed = False

    def start(self) -> dict[str, Any]:
        self.started = True
        self.requests.append(("initialize", {}))
        return {"serverInfo": {"name": "fake-app-server", "version": "test"}}

    def close(self) -> None:
        self.closed = True

    def model_list(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("model/list", dict(kwargs)))
        return {"data": self.models, "nextCursor": None}

    def thread_start(self, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("thread/start", dict(kwargs)))
        self.emit("thread/started", {"thread": {"id": self.thread_id}})
        return {"thread": {"id": self.thread_id}}

    def thread_resume(self, thread_id: str, **kwargs: Any) -> dict[str, Any]:
        self.requests.append(("thread/resume", {"threadId": thread_id, **kwargs}))
        self.emit("thread/started", {"thread": {"id": thread_id}})
        return {"thread": {"id": thread_id}}

    def turn_start(self, thread_id: str, instruction: str, **kwargs: Any) -> dict[str, Any]:
        self.turn_id = f"fake-turn-{self.next_turn_number}"
        self.next_turn_number += 1
        self.requests.append(("turn/start", {"threadId": thread_id, "instruction": instruction, **kwargs}))
        self.emit("turn/started", {"threadId": thread_id, "turn": {"id": self.turn_id, "status": "inProgress"}})
        if self.auto_complete:
            self.emit("item/agentMessage/delta", {"threadId": thread_id, "turnId": self.turn_id, "itemId": "item-1", "delta": self.final_message})
            self.emit("turn/completed", {"threadId": thread_id, "turn": {"id": self.turn_id, "status": "completed", "items": []}})
        return {"turn": {"id": self.turn_id, "status": "inProgress"}}

    def turn_steer(self, thread_id: str, turn_id: str, instruction: str) -> dict[str, Any]:
        self.requests.append(("turn/steer", {"threadId": thread_id, "turnId": turn_id, "instruction": instruction}))
        return {"turnId": turn_id}

    def turn_interrupt(self, thread_id: str, turn_id: str) -> dict[str, Any]:
        self.requests.append(("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}))
        self.emit("turn/completed", {"threadId": thread_id, "turn": {"id": turn_id, "status": "interrupted", "items": []}})
        return {}

    def emit(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        event = {"method": method, "params": params or {}}
        self.notifications.append(event)
        if self.notification_handler:
            self.notification_handler(event)

    def fail(self, message: str = "fake process failed") -> None:
        error = RuntimeError(message)
        if self.process_error_handler:
            self.process_error_handler(error)


FakeCodexAppServer = FakeAppServer
