from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from ..store.task_store import TaskStore, utc_now
from .state_machine import InvalidTaskTransition, TaskStateMachine


class TaskEventBus:
    """Monotonic, persisted, safe event stream for one task."""

    _locks_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}
    _TRANSITION_CONTROLLED_FIELDS = frozenset(
        {
            "state",
            "status",
            "from",
            "to",
            "reason",
            "stage",
            "current_action",
            "last_transition_reason",
            "event_seq",
            "last_event_seq",
        }
    )

    def __init__(self, store: TaskStore, task_id: str, *, subscribers: Optional[list[Callable[[dict[str, Any]], None]]] = None):
        self.store = store
        self.task_id = task_id
        self._lock = self._lock_for(store, task_id)
        self._subscribers = list(subscribers or [])

    @classmethod
    def _lock_for(cls, store: TaskStore, task_id: str) -> threading.RLock:
        lock_key = str(store.root.resolve()) + "::" + task_id
        with cls._locks_guard:
            return cls._locks.setdefault(lock_key, threading.RLock())

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def emit(self, event_type: str, data: Optional[dict[str, Any]] = None, *, task_id: Optional[str] = None) -> dict[str, Any]:
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event type must be non-empty")
        if event_type == "state_changed":
            raise ValueError("state_changed events require TaskEventBus.transition")
        event_task_id = task_id or self.task_id
        event_lock = self._lock_for(self.store, event_task_id)
        with event_lock, self.store.task_lock(event_task_id):
            event = self._append_event_locked(event_task_id, event_type, data or {})
        with self._lock:
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
        transition_data = self._validated_transition_updates(updates)
        event_lock = self._lock_for(self.store, event_task_id)
        with event_lock, self.store.task_lock(event_task_id):
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
                {"from": from_state, "to": to_state, "reason": reason, **transition_data},
                persist_cursor=False,
            )
            if from_state is not None:
                transition_updates = {**transition_data, "event_seq": event["seq"], "last_event_seq": event["seq"]}
                self.store._apply_transition(event_task_id, from_state, to_state, reason, updates=transition_updates)
            else:
                self.store.update_task(event_task_id, event_seq=event["seq"], last_event_seq=event["seq"])
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(dict(event))
            except Exception:
                continue
        return event

    def reconcile(self) -> None:
        """Replay journaled transitions left incomplete by a process crash."""
        with self._lock, self.store.task_lock(self.task_id):
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
                    transition_updates = {
                        key: value
                        for key, value in data.items()
                        if key not in {"from", "to", "reason"}
                    }
                    transition_updates.update(
                        event_seq=int(event.get("seq", 0)),
                        last_event_seq=int(event.get("seq", 0)),
                    )
                    self.store._apply_transition(
                        self.task_id,
                        current,
                        target,
                        str(data.get("reason") or data.get("action") or "recovered state transition"),
                        updates=transition_updates,
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

    def _append_event_locked(
        self,
        task_id: str,
        event_type: str,
        data: dict[str, Any],
        *,
        persist_cursor: bool = True,
    ) -> dict[str, Any]:
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
        if persist_cursor:
            self.store.update_task(task_id, event_seq=seq, last_event_seq=seq)
        return event

    @classmethod
    def _validated_transition_updates(cls, updates: Optional[dict[str, Any]]) -> dict[str, Any]:
        if updates is None:
            return {}
        if not isinstance(updates, dict):
            raise ValueError("transition updates must be an object")
        reserved = cls._TRANSITION_CONTROLLED_FIELDS.intersection(updates)
        if reserved:
            names = ", ".join(sorted(reserved))
            raise ValueError(f"transition updates cannot replace reserved lifecycle fields: {names}")
        return dict(updates)

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
