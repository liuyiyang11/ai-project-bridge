from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from ..store.task_store import TaskStore, utc_now
from .state_machine import InvalidTaskTransition, TaskStateMachine


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
            event = self._append_event_locked(event_task_id, event_type, data or {})
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(dict(event))
            except Exception:
                continue
        return event

    def transition(
        self,
        from_state: Optional[str],
        to_state: str,
        reason: str,
        *,
        task_id: Optional[str] = None,
        updates: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Validate and persist one task state transition plus its event."""
        event_task_id = task_id or self.task_id
        from_state = getattr(from_state, "value", from_state)
        to_state = getattr(to_state, "value", to_state)
        with self._lock:
            current = self.store.get_task(event_task_id).get("state")
            if from_state is None:
                if current != to_state:
                    raise InvalidTaskTransition(f"invalid task transition: {current} -> {to_state}")
            else:
                if current != from_state:
                    raise InvalidTaskTransition(f"invalid task transition: {current} -> {to_state}; found {current}")
                TaskStateMachine.require(from_state, to_state)
            event = self._append_event_locked(
                event_task_id,
                "state_changed",
                {"from": from_state, "to": to_state, "reason": reason, **(updates or {})},
            )
            if from_state is not None:
                self.store.transition_task(event_task_id, from_state, to_state, reason)
            if updates:
                self.store.update_task(event_task_id, **updates)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(dict(event))
            except Exception:
                continue
        return event

    def reconcile(self) -> None:
        """Replay journaled transitions left incomplete by a process crash."""
        with self._lock:
            snapshot = self.store.get_task(self.task_id)
            current = str(snapshot.get("state", snapshot.get("status", "")))
            events = self.store.all_events(self.task_id)
            ordered = sorted(
                (event for event in events if isinstance(event, dict)),
                key=lambda event: int(event.get("seq", 0)) if str(event.get("seq", "")).isdigit() else 0,
            )
            for event in ordered:
                if event.get("type") != "state_changed" or not isinstance(event.get("data"), dict):
                    continue
                data = event["data"]
                target = data.get("to")
                source = data.get("from")
                if not isinstance(target, str) or target == current:
                    continue
                if source != current and not TaskStateMachine.can_transition(current, target):
                    continue
                try:
                    self.store.transition_task(
                        self.task_id,
                        current,
                        target,
                        str(data.get("reason") or data.get("action") or "recovered state transition"),
                    )
                except (InvalidTaskTransition, ValueError):
                    continue
                current = target
            max_seq = max(
                (int(event.get("seq", 0)) for event in ordered if str(event.get("seq", "")).isdigit()),
                default=0,
            )
            recorded_seq = int(snapshot.get("last_event_seq", snapshot.get("event_seq", 0)))
            if max_seq > recorded_seq:
                self.store.update_task(self.task_id, event_seq=max_seq, last_event_seq=max_seq)

    def events(self, *, after_seq: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.list_events(self.task_id, after_seq=after_seq, limit=limit)

    def _append_event_locked(self, task_id: str, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
        state = self.store.get_task(task_id)
        journal_seq = max(
            (int(event.get("seq", 0)) for event in self.store.all_events(task_id) if str(event.get("seq", "")).isdigit()),
            default=0,
        )
        seq = max(int(state.get("last_event_seq", state.get("event_seq", 0))), journal_seq) + 1
        event = {
            "seq": seq,
            "task_id": task_id,
            "type": event_type,
            "time": utc_now(),
            "data": self._sanitize(data),
        }
        self.store.append_event(task_id, event)
        self.store.update_task(task_id, event_seq=seq, last_event_seq=seq)
        return event

    @classmethod
    def _sanitize(cls, value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "<truncated>"
        if isinstance(value, dict):
            blocked = {"reasoning", "rawreasoning", "chainofthought", "textdelta", "raw_chain_of_thought", "raw_reasoning"}
            return {
                str(key): cls._sanitize(item, depth + 1)
                for key, item in value.items()
                if str(key).casefold() not in blocked
            }
        if isinstance(value, list):
            return [cls._sanitize(item, depth + 1) for item in value[:100]]
        if isinstance(value, str):
            return value[:16000]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:1000]
