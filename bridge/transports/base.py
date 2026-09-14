from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseTransport(ABC):
    """Transport adapter boundary; orchestration lives outside transports."""

    @abstractmethod
    def serve(self) -> Any:
        raise NotImplementedError
