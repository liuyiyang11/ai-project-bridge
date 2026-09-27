"""One bounded, read-only subprocess call for a quote and recent daily K-lines."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from ..config import MarketDataConfig
from .errors import MarketDataRequestError, MarketDataSourceError, MarketDataTimeoutError


logger = logging.getLogger(__name__)
_SYMBOL = re.compile(r"^[0-9]{6}$")
_STOCK_NAME = re.compile(r"^[\u3400-\u9fffA-Za-z0-9.*()（）\-]{1,32}$")
_MAX_STDOUT_BYTES = 1_000_000
_QUOTE_FIELDS = {
    "price": "price",
    "last_close": "last_close",
    "open": "open",
    "high": "high",
    "low": "low",
    "change_amt": "change",
    "change_pct": "change_pct",
    "amount_wan": "amount_wan",
}
_BAR_FIELDS = ("open", "high", "low", "close", "volume")
_CN_TZ = timezone(timedelta(hours=8))


class MarketDataService:
    """Run the a-stock-data quote and K-line functions in one isolated process."""

    def __init__(
        self,
        config: Optional[MarketDataConfig],
        *,
        runner: Callable[..., Any] = subprocess.run,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.config = config
        self._runner = runner
        self._clock = clock

    def snapshot(self, symbol: str, days: int = 20, adjust: str = "qfq") -> dict[str, Any]:
        """Return a public-safe snapshot or raise a typed, non-sensitive error."""
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MarketDataRequestError()
        if (
            type(days) is not int
            or not 1 <= days <= 250
            or not isinstance(adjust, str)
            or adjust not in {"qfq", "hfq", "none"}
        ):
            raise MarketDataRequestError()

        config = self.config
        if config is None or not config.enabled or config.root is None or config.python_executable is None:
            raise MarketDataSourceError()
        root = Path(config.root)
        python_executable = Path(config.python_executable)
        adapter = Path(__file__).with_name("adapter.py").resolve()
        if not root.is_dir() or not (root / "SKILL.md").is_file() or not python_executable.is_file():
            raise MarketDataSourceError()

        argv = [
            str(python_executable),
            "-B",
            "-X",
            "utf8",
            str(adapter),
            "--repo-root",
            str(root),
            "--symbol",
            symbol,
            "--days",
            str(days),
            "--adjust",
            adjust,
        ]
        started = self._clock()
        try:
            completed = self._runner(
                argv,
                cwd=str(root),
                env=self._child_environment(),
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=config.default_timeout_seconds,
                check=False,
                close_fds=True,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("market data subprocess timed out")
            raise MarketDataTimeoutError() from exc
        except OSError as exc:
            logger.warning("market data subprocess could not start error_type=%s", type(exc).__name__)
            raise MarketDataSourceError() from exc

        total_ms = self._elapsed_ms(started)
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        if completed.returncode != 0:
            logger.warning(
                "market data subprocess exited nonzero returncode=%s stderr_chars=%s",
                completed.returncode,
                len(stderr),
            )
            raise MarketDataSourceError()
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_STDOUT_BYTES:
            logger.warning("market data subprocess output exceeded limit")
            raise MarketDataSourceError()
        try:
            payload = json.loads(stdout)
        except (json.JSONDecodeError, TypeError):
            logger.warning("market data subprocess returned invalid JSON")
            raise MarketDataSourceError() from None

        result = self._normalize(payload, symbol=symbol, days=days, adjust=adjust)
        result["latency_ms"]["total"] = total_ms
        return result

    def _child_environment(self) -> dict[str, str]:
        """Pass only OS/proxy essentials; credential-bearing variables stay out of the child."""
        allowed = (
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
        )
        return {key: os.environ[key] for key in allowed if key in os.environ}

    def _normalize(self, payload: Any, *, symbol: str, days: int, adjust: str) -> dict[str, Any]:
        """Allowlist every public field; never forward an adapter object wholesale."""
        try:
            if not isinstance(payload, dict) or payload.get("symbol") != symbol:
                raise ValueError("invalid adapter result")
            if payload.get("adjust") != adjust:
                raise ValueError("adjustment mismatch")
            if payload.get("data_source") != {"quote": "tencent", "daily_kline": "tencent"}:
                raise ValueError("unexpected data source")

            name = payload.get("name")
            if not isinstance(name, str) or not _STOCK_NAME.fullmatch(name):
                raise ValueError("invalid stock name")
            raw_quote = payload.get("quote")
            if not isinstance(raw_quote, dict) or raw_quote.get("is_stale") is True:
                raise ValueError("invalid quote")
            if raw_quote.get("name") != name:
                raise ValueError("quote name mismatch")
            quote: dict[str, float] = {}
            for source_key, public_key in _QUOTE_FIELDS.items():
                if source_key not in raw_quote:
                    if source_key in {"price", "last_close", "open", "high", "low", "change_amt", "change_pct"}:
                        raise ValueError("missing quote field")
                    continue
                quote[public_key] = self._finite_number(raw_quote[source_key])

            raw_bars = payload.get("daily_kline")
            if not isinstance(raw_bars, list) or not 1 <= len(raw_bars) <= days:
                raise ValueError("invalid daily K-line rows")
            daily_kline: list[dict[str, Any]] = []
            seen_dates: set[str] = set()
            previous = ""
            for raw_bar in raw_bars:
                if not isinstance(raw_bar, dict):
                    raise ValueError("invalid daily K-line row")
                day = raw_bar.get("date")
                if (
                    not isinstance(day, str)
                    or date.fromisoformat(day).isoformat() != day
                    or day in seen_dates
                    or day < previous
                ):
                    raise ValueError("invalid daily K-line date")
                row: dict[str, Any] = {"date": day}
                for field in _BAR_FIELDS:
                    if field not in raw_bar:
                        raise ValueError("missing daily K-line field")
                    row[field] = self._finite_number(raw_bar[field])
                if row["volume"] < 0:
                    raise ValueError("invalid volume")
                seen_dates.add(day)
                previous = day
                daily_kline.append(row)

            retrieved_at = payload.get("retrieved_at")
            if not isinstance(retrieved_at, str):
                raise ValueError("invalid retrieval time")
            parsed_time = datetime.fromisoformat(retrieved_at.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                raise ValueError("retrieval time must include timezone")

            market_date = parsed_time.astimezone(_CN_TZ).date().isoformat()
            context_date = self._optional_date(payload.get("market_date"))
            expected_trade_date = self._optional_date(payload.get("expected_latest_trade_date"))
            market_status = payload.get("market_status")
            if not isinstance(market_status, str) or market_status not in {"OPEN", "CLOSED", "UNKNOWN"}:
                market_status = "UNKNOWN"
            if context_date != market_date:
                expected_trade_date = None
                market_status = "UNKNOWN"
            if expected_trade_date is not None and expected_trade_date > market_date:
                expected_trade_date = None
                market_status = "UNKNOWN"

            quote_timestamp = self._optional_timestamp(payload.get("quote_timestamp"))
            quote_date = (
                quote_timestamp.astimezone(_CN_TZ).date().isoformat()
                if quote_timestamp is not None
                else None
            )
            daily_kline_last_date = daily_kline[-1]["date"]
            quote_stale = (
                quote_date < expected_trade_date
                if quote_date is not None and expected_trade_date is not None
                else None
            )
            daily_kline_stale = (
                daily_kline_last_date < expected_trade_date
                if expected_trade_date is not None
                else None
            )
            date_consistent = (
                quote_date == daily_kline_last_date
                if quote_date is not None
                else None
            )
            warnings: list[str] = []
            if daily_kline_stale is True:
                warnings.append("daily kline is behind the latest expected trading date")
            if quote_stale is True:
                warnings.append("quote is behind the latest expected trading date")
            if date_consistent is False:
                warnings.append("quote and daily K-line dates do not match")

            timings = payload.get("_timings_ms")
            if not isinstance(timings, dict):
                raise ValueError("missing timings")
            quote_ms = self._finite_number(timings.get("quote"))
            kline_ms = self._finite_number(timings.get("daily_kline"))
            if quote_ms < 0 or kline_ms < 0:
                raise ValueError("invalid timings")

            return {
                "symbol": symbol,
                "name": name,
                "quote": quote,
                "daily_kline": daily_kline,
                "adjust": adjust,
                "data_source": {"quote": "tencent", "daily_kline": "tencent"},
                "retrieved_at": parsed_time.isoformat(),
                "market_date": market_date,
                "quote_timestamp": quote_timestamp.isoformat() if quote_timestamp is not None else None,
                "daily_kline_last_date": daily_kline_last_date,
                "expected_latest_trade_date": expected_trade_date,
                "market_status": market_status,
                "data_freshness": {
                    "quote_stale": quote_stale,
                    "daily_kline_stale": daily_kline_stale,
                    "date_consistent": date_consistent,
                },
                "latency_ms": {"quote": quote_ms, "daily_kline": kline_ms},
                "warnings": warnings,
            }
        except (KeyError, TypeError, ValueError, OverflowError):
            logger.warning("market data subprocess output failed validation")
            raise MarketDataSourceError() from None

    @staticmethod
    def _finite_number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected numeric value")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("expected finite numeric value")
        return number

    @staticmethod
    def _optional_date(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError:
            return None
        return normalized if normalized == value else None

    @staticmethod
    def _optional_timestamp(value: Any) -> Optional[datetime]:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed

    def _elapsed_ms(self, started: float) -> float:
        return round(max(0.0, (self._clock() - started) * 1000), 3)
