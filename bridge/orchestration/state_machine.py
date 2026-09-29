from __future__ import annotations

from typing import Iterable, Union

from ..codex.session import SessionState


class InvalidTaskTransition(RuntimeError):
    pass


class TaskStateMachine:
    """Central transition policy shared by transports and control tools."""

    _TRANSITIONS = {
        SessionState.QUEUED: {SessionState.PREPARING, SessionState.FAILED, SessionState.CANCELLED},
        SessionState.PREPARING: {SessionState.RUNNING, SessionState.FAILED, SessionState.INTERRUPTED, SessionState.UNKNOWN},
        SessionState.RUNNING: {SessionState.WAITING_REVIEW, SessionState.INTERRUPTED, SessionState.FAILED, SessionState.UNKNOWN},
        SessionState.WAITING_REVIEW: {SessionState.RUNNING, SessionState.COMPLETED},
        SessionState.COMPLETED: set(),
        SessionState.INTERRUPTED: {SessionState.PREPARING, SessionState.FAILED},
        SessionState.FAILED: {SessionState.PREPARING},
        SessionState.UNKNOWN: {SessionState.RUNNING, SessionState.CANCELLED},
        SessionState.CANCELLED: set(),
    }

    @classmethod
    def can_transition(cls, old: Union[str, SessionState], new: Union[str, SessionState]) -> bool:
        old_state = SessionState(old)
        new_state = SessionState(new)
        return new_state in cls._TRANSITIONS[old_state]

    @classmethod
    def require(cls, old: Union[str, SessionState], new: Union[str, SessionState]) -> None:
        if not cls.can_transition(old, new):
            raise InvalidTaskTransition(f"invalid task transition: {SessionState(old).value} -> {SessionState(new).value}")

    @classmethod
    def allowed(cls, old: Union[str, SessionState]) -> Iterable[str]:
        return [item.value for item in cls._TRANSITIONS[SessionState(old)]]
