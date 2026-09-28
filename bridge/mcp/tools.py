from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from ..orchestration.router import TaskRequest
from ..orchestration.supervisor import TaskSupervisor
from ..public_errors import (
    public_error_from_exception,
    public_error_from_message,
    sanitize_public_events,
    sanitize_public_status,
)
from ..security import validate_unicode_scalars
from ..market_data import MarketContextService, MarketDataService
from ..market_data.errors import MarketDataRequestError, MarketDataSourceError, MarketDataTimeoutError
from .schemas import (
    EmptyInput,
    StartCodeInput,
    StartExperimentInput,
    StartPresentationInput,
    TaskArtifactsInput,
    TaskControlInput,
    TaskEventsInput,
    TaskStatusInput,
    MarketSnapshotInput,
    MarketContextInput,
)


class McpToolError(ValueError):
    """A safe, caller-facing MCP tool validation or execution error."""

    def __init__(self, message: str, *, public_error: Any = None):
        super().__init__(message)
        self.public_error = public_error


logger = logging.getLogger(__name__)


class BridgeMcpTools:
    """The local Bridge tools exposed by the stdio adapter."""

    _SCHEMAS: dict[str, Type[BaseModel]] = {
        "bridge_list_projects": EmptyInput,
        "bridge_codex_catalog": EmptyInput,
        "bridge_start_code_task": StartCodeInput,
        "bridge_start_experiment_review": StartExperimentInput,
        "bridge_start_presentation_task": StartPresentationInput,
        "bridge_task_status": TaskStatusInput,
        "bridge_task_events": TaskEventsInput,
        "bridge_control_task": TaskControlInput,
        "bridge_task_artifacts": TaskArtifactsInput,
        "bridge_market_snapshot": MarketSnapshotInput,
        "bridge_market_context": MarketContextInput,
    }

    def __init__(
        self,
        config: Any = None,
        *,
        supervisor: Optional[TaskSupervisor] = None,
        market_data_service: Optional[MarketDataService] = None,
        market_context_service: Optional[MarketContextService] = None,
    ):
        if supervisor is None and config is None:
            raise ValueError("config or supervisor is required")
        self.supervisor = supervisor or TaskSupervisor(config)
        self.market_data_service = market_data_service or MarketDataService(getattr(config, "market_data", None))
        self.market_context_service = market_context_service or MarketContextService(getattr(config, "market_data", None))

    @classmethod
    def definitions(cls) -> list[dict[str, Any]]:
        descriptions = {
            "bridge_list_projects": "List registered projects and public capabilities.",
            "bridge_codex_catalog": "List runtime Codex models and supported reasoning efforts.",
            "bridge_start_code_task": "Start a code task in an isolated registered-project worktree.",
            "bridge_start_experiment_review": "Run a registered deterministic experiment review against a trusted candidate.",
            "bridge_start_presentation_task": "Start a presentation task in an isolated registered-project worktree.",
            "bridge_task_status": "Read safe status for a Bridge task.",
            "bridge_task_events": "Read a bounded incremental safe event stream for a Bridge task.",
            "bridge_control_task": "Steer, interrupt, continue, or accept a Bridge task.",
            "bridge_task_artifacts": "Read a bounded artifact manifest for a Bridge task.",
            "bridge_market_snapshot": (
                "Deterministic, synchronous, read-only fast path for one A-share quote and recent daily K-lines. "
                "Uses the configured a-stock-data adapter in one bounded Python subprocess, returns public-safe "
                "structured data, does not start a Codex task or enter WorkerQueue, and provides no investment advice. "
                "daily_kline preserves the requested adjustment and source values. latest_price_bar is an "
                "unadjusted latest-price reference when available. For exact prices, use latest_price_bar only "
                "after checking data_quality.latest_daily_bar.freshness_status; use daily_kline[-1] for exact "
                "prices only when data_quality.latest_daily_bar.exact_price_safe is true."
            ),
            "bridge_market_context": (
                "Deterministic, synchronous, read-only context for completed-trading-day review of ETFs present in "
                "the verified local instrument registry. Historical trade dates never include a current quote. "
                "Returns target-date Tencent 5-minute and daily bars; snapshot is unavailable for historical dates. "
                "ETF-share enrichment may fail. Does not call TaskSupervisor, start or use Codex, enter WorkerQueue, "
                "or provide investment advice."
            ),
        }
        return [
            {"name": name, "description": descriptions[name], "inputSchema": schema.schema()}
            for name, schema in cls._SCHEMAS.items()
        ]

    def call(self, name: str, arguments: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        schema = self._SCHEMAS.get(name)
        if schema is None:
            raise McpToolError("unknown Bridge tool")
        try:
            value = schema.parse_obj(arguments or {})
        except ValidationError as exc:
            public = public_error_from_exception(exc, context="mcp")
            if schema in {MarketSnapshotInput, MarketContextInput}:
                raise McpToolError(public.message, public_error=public) from None
            raise McpToolError(public.message) from None
        try:
            validate_unicode_scalars(self._model_values(value))
            handler = getattr(self, name)
            return handler(value)
        except McpToolError as exc:
            public = public_error_from_exception(exc, context="mcp")
            raise McpToolError(public.message) from None
        except Exception as exc:
            logger.error(
                "MCP tool failed tool=%s error_type=%s",
                name,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            public = public_error_from_exception(exc, context="mcp")
            if isinstance(exc, (MarketDataRequestError, MarketDataTimeoutError, MarketDataSourceError)):
                raise McpToolError(public.message, public_error=public) from None
            raise McpToolError(public.message) from None

    def bridge_list_projects(self, value: EmptyInput) -> dict[str, Any]:
        return {"projects": self.supervisor.list_projects()}

    def bridge_codex_catalog(self, value: EmptyInput) -> dict[str, Any]:
        return self.supervisor.codex_catalog()

    def bridge_start_code_task(self, value: StartCodeInput) -> dict[str, Any]:
        return self.supervisor.start_code_task(
            value.project,
            value.instruction,
            value.acceptance,
            model=value.model,
            reasoning_effort=value.reasoning_effort,
        )

    def bridge_start_experiment_review(self, value: StartExperimentInput) -> dict[str, Any]:
        return self.supervisor.start_experiment_review(
            value.project,
            value.command_id,
            source_task_id=value.source_task_id,
            review=value.review,
        )

    def bridge_start_presentation_task(self, value: StartPresentationInput) -> dict[str, Any]:
        return self.supervisor.start_presentation_task(
            value.project,
            value.instruction,
            brief=value.brief,
            slides_spec=value.slides_spec,
            assets_dir=value.assets_dir,
            template=value.template,
            renderer=value.renderer,
            model=value.model,
            reasoning_effort=value.reasoning_effort,
        )

    def bridge_task_status(self, value: TaskStatusInput) -> dict[str, Any]:
        return sanitize_public_status(self.supervisor.task_status(value.task_id))

    def bridge_task_events(self, value: TaskEventsInput) -> dict[str, Any]:
        events = self.supervisor.task_events(value.task_id, after_seq=value.after_seq, limit=value.limit)
        return {"task_id": value.task_id, "events": sanitize_public_events(events)}

    def bridge_control_task(self, value: TaskControlInput) -> dict[str, Any]:
        return self.supervisor.control_task(value.task_id, value.action, value.instruction)

    def bridge_task_artifacts(self, value: TaskArtifactsInput) -> dict[str, Any]:
        return {"task_id": value.task_id, "artifacts": self.supervisor.task_artifacts(value.task_id, kind=value.kind, limit=value.limit)}

    def bridge_market_snapshot(self, value: MarketSnapshotInput) -> dict[str, Any]:
        return self.market_data_service.snapshot(value.symbol, value.days, value.adjust)

    def bridge_market_context(self, value: MarketContextInput) -> dict[str, Any]:
        return self.market_context_service.market_context(value.symbol, value.trade_date)

    @staticmethod
    def _model_values(value: BaseModel) -> Any:
        """Return model data on both Pydantic v1 and v2."""
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return model_dump()
        as_dict = getattr(value, "dict", None)
        if callable(as_dict):
            return as_dict()
        return value

    @staticmethod
    def _safe_error(message: str) -> str:
        return public_error_from_message(message).message
