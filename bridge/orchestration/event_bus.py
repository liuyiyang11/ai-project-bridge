from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from ..task_store import TaskStore, utc_now


class TaskEventBus:
    """Monotonic, persisted, safe event stream for one task."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, store: TaskStore, task_id: str, *, subscribers: Optional[list[Callable[[dict[str, Any]], None]]] = None):
        self.store = store
        self.task_id = task_id
        lock_key = str(store.root.resolve()) + "::" + task_id
        with self._locks_guard:
            self._lock = self._locks.setdefault(lock_key, threading.RLock())
        self._subscribers = list(subscribers or [])

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def emit(self, event_type: str, data: Optional[dict[str, Any]] = None, *, task_id: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event type must be non-empty")
        event_task_id = task_id or self.task_id
        with self._lock:
            state = self.store.load_state(event_task_id)
            seq = int(state.get("event_seq", 0)) + 1
            event = {
                "seq": seq,
                "type": event_type,
                "task_id": event_task_id,
                "timestamp": utc_now(),
                "data": self._sanitize(data or {}),
            }
            self.store.append_event(event_task_id, event)
            self.store.update_state(event_task_id, event_seq=seq, last_event_seq=seq)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(dict(event))
            except Exception:
                continue
        return event

    def events(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.read_events(self.task_id, after_seq=after_seq, limit=limit)

    @classmethod
    def _sanitize(cls, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "<truncated>"
        if isinstance(value, dict):
            blocked = {"reasoning", "rawReasoning", "chainOfThought", "textDelta", "raw_chain_of_thought"}
            return {str(key): cls._sanitize(item, depth + 1) for key, item in value.items() if key not in blocked}
        if isinstance(value, list):
            return [cls._sanitize(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value[:16000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:1000]
