from __future__ import annotations

from typing import Any

from ..dispatcher import Dispatcher
from .base import BaseTransport


class GitHubTransport(BaseTransport):
    """Compatibility adapter for the GitHub Issue transport.

    The Dispatcher keeps Issue parsing, trust checks, and GitHub publication;
    code tasks delegate their lifecycle to the shared Supervisor runtime.
    Presentation and experiment-review retain their mature compatibility
    paths for this release.
    """

    def __init__(self, dispatcher: Dispatcher):
        self.dispatcher = dispatcher

    def serve(self) -> list[dict[str, Any]]:
        return self.dispatcher.run_once()
