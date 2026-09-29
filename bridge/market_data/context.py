"""Validated public contract for the deterministic ETF market-context fast path."""

from __future__ import annotations

from datetime import date, datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Optional

from ..config import MarketDataConfig
from .errors import MarketDataRequestError, MarketDataSourceError, MarketDataTimeoutError


logger = logging.getLogger(__name__)
_SYMBOL = re.compile(r"^[0-9]{6}$")
_NAME = re.compile(r"^[\u3400-\u9fffA-Za-z0-9.*+·()（）\-]{1,64}$")
_STATUS = {"OK", "PARTIAL", "UNAVAILABLE", "TIMEOUT", "ERROR", "UNKNOWN"}
_FRESHNESS = {"FRESH", "STALE", "PARTIAL", "UNKNOWN"}
_EVIDENCE = {"LEVEL_A_RAW_FACT", "LEVEL_B_VENDOR_DERIVED", "LEVEL_C_MODEL_DERIVED"}
_WARNINGS = {
    "SOURCE_TIMEOUT",
    "SOURCE_UNAVAILABLE",
    "TARGET_DATE_NOT_RETURNED",
    "SOURCE_BEHIND_REQUESTED_DATE",
    "LESS_THAN_60_BARS_AVAILABLE",
}
_MAX_CONTEXT_BYTES = 40 * 1024
_MAX_CONTEXT_STDOUT_BYTES = 40 * 1024
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
_BLOCK_KEYS = ("snapshot", "intraday_5m", "daily")


class MarketContextService:
    """Run one bounded a-stock-data subprocess and expose only allowlisted facts."""

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
        self._registry = self._load_instrument_registry()

    @staticmethod
    def _load_instrument_registry() -> dict[str, Any]:
        path = Path(__file__).with_name("instrument_registry.json")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or set(raw) != {"version", "instruments", "market_references"}:
                raise ValueError("invalid instrument registry")
            if raw.get("version") != 1 or not isinstance(raw.get("instruments"), dict):
                raise ValueError("invalid instrument registry version")
            for symbol, item in raw["instruments"].items():
                if (
                    not isinstance(symbol, str)
                    or not _SYMBOL.fullmatch(symbol)
                    or not isinstance(item, dict)
                    or set(item) != {"symbol", "asset_type", "exchange", "name", "verification_status", "tracking_index"}
                    or item.get("symbol") != symbol
                    or item.get("asset_type") != "ETF"
                    or item.get("exchange") not in {"SH", "SZ"}
                    or not isinstance(item.get("name"), str)
                    or not _NAME.fullmatch(item["name"])
                    or item.get("verification_status") != "VERIFIED"
                ):
                    raise ValueError("invalid verified instrument entry")
                tracking = item.get("tracking_index")
                if tracking is not None and (
                    not isinstance(tracking, dict)
                    or set(tracking) != {"code", "name", "verification_status"}
                    or not isinstance(tracking.get("code"), str)
                    or not _SYMBOL.fullmatch(tracking["code"])
                    or not isinstance(tracking.get("name"), str)
                    or not _NAME.fullmatch(tracking["name"])
                    or tracking.get("verification_status") != "VERIFIED"
                ):
                    raise ValueError("invalid verified tracking index entry")
            refs = raw.get("market_references")
            if not isinstance(refs, list) or len(refs) > 3:
                raise ValueError("invalid market references")
            for item in refs:
                if (
                    not isinstance(item, dict)
                    or set(item) != {"code", "name", "role"}
                    or not isinstance(item.get("code"), str)
                    or not _SYMBOL.fullmatch(item["code"])
                    or not isinstance(item.get("name"), str)
                    or not _NAME.fullmatch(item["name"])
                    or item.get("role") != "MARKET_REFERENCE"
                ):
                    raise ValueError("invalid market reference")
            return raw
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            logger.error("market context instrument registry failed validation")
            raise MarketDataSourceError() from None

    def market_context(self, symbol: str, trade_date: Optional[str] = None) -> dict[str, Any]:
        self._validate_request(symbol, trade_date)
        config = self.config
        if (
            config is None
            or not config.enabled
            or config.root is None
            or config.python_executable is None
        ):
            raise MarketDataSourceError()
        root = Path(config.root)
        python_executable = Path(config.python_executable)
        instrument = self._registry["instruments"][symbol]
        adapter = Path(__file__).with_name("context_adapter.py").resolve()
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
            "--exchange",
            instrument["exchange"],
        ]
        if trade_date is not None:
            argv.extend(("--trade-date", trade_date))
        started = self._clock()
        timeout = config.market_context_timeout_seconds
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
                timeout=timeout,
                check=False,
                close_fds=True,
            )
        except subprocess.TimeoutExpired as exc:
            logger.warning("market context subprocess timed out")
            raise MarketDataTimeoutError() from exc
        except OSError as exc:
            logger.warning("market context subprocess could not start error_type=%s", type(exc).__name__)
            raise MarketDataSourceError() from exc

        elapsed_ms = self._elapsed_ms(started)
        stdout = completed.stdout if isinstance(completed.stdout, str) else ""
        stderr = completed.stderr if isinstance(completed.stderr, str) else ""
        if completed.returncode != 0:
            logger.warning(
                "market context subprocess exited nonzero returncode=%s stderr_chars=%s",
                completed.returncode,
                len(stderr),
            )
            raise MarketDataSourceError()
        if len(stdout.encode("utf-8", errors="replace")) > _MAX_CONTEXT_STDOUT_BYTES:
            logger.warning("market context subprocess output exceeded hard limit")
            raise MarketDataSourceError()
        try:
            payload = json.loads(stdout, parse_constant=self._reject_constant)
        except (json.JSONDecodeError, TypeError, ValueError):
            logger.warning("market context subprocess returned invalid JSON")
            raise MarketDataSourceError() from None

        if payload == {"request_error": "INVALID_TRADE_DATE"}:
            raise MarketDataRequestError()

        result = self._normalize(payload, symbol=symbol, requested_trade_date=trade_date, elapsed_ms=elapsed_ms)
        result["latency_ms"]["total"] = self._elapsed_ms(started)
        serialized = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(serialized.encode("utf-8")) > _MAX_CONTEXT_BYTES:
            logger.warning("normalized market context exceeded hard payload limit")
            raise MarketDataSourceError()
        return result

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def _validate_request(self, symbol: str, trade_date: Optional[str]) -> None:
        if not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol):
            raise MarketDataRequestError()
        instrument = self._registry["instruments"].get(symbol)
        if (
            not isinstance(instrument, dict)
            or instrument.get("asset_type") != "ETF"
            or instrument.get("verification_status") != "VERIFIED"
        ):
            raise MarketDataRequestError()
        if trade_date is None:
            return
        if not isinstance(trade_date, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", trade_date):
            raise MarketDataRequestError()
        try:
            if date.fromisoformat(trade_date).isoformat() != trade_date:
                raise MarketDataRequestError()
        except ValueError:
            raise MarketDataRequestError() from None

    @staticmethod
    def _child_environment() -> dict[str, str]:
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

    def _normalize(
        self,
        payload: Any,
        *,
        symbol: str,
        requested_trade_date: Optional[str],
        elapsed_ms: float,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("symbol") != symbol:
            raise MarketDataSourceError()
        target = self._date(payload.get("trade_date"))
        previous = self._date(payload.get("previous_trade_date"))
        if target is None or previous is None or previous >= target:
            raise MarketDataSourceError()
        if requested_trade_date is not None and target != requested_trade_date:
            raise MarketDataSourceError()
        retrieved_at = self._timestamp(payload.get("retrieved_at"))
        raw_core = payload.get("core")
        if not isinstance(raw_core, dict) or set(raw_core) != set(_BLOCK_KEYS):
            raise MarketDataSourceError()

        snapshot = self._snapshot_block(raw_core["snapshot"], target)
        intraday = self._intraday_block(raw_core["intraday_5m"], target)
        daily = self._daily_block(raw_core["daily"], target)
        core_blocks = {"snapshot": snapshot, "intraday_5m": intraday, "daily": daily}
        if intraday["status"] != "OK" or daily["status"] != "OK":
            raise MarketDataSourceError()

        shares, share_timings = self._shares_block(payload.get("etf_shares"), symbol, target, previous)
        overall_status = "OK" if snapshot["status"] == "OK" and shares["status"] == "OK" else "PARTIAL"
        raw_timings = payload.get("_timings_ms")
        timings = self._normalize_timings(raw_timings)
        timings.update(share_timings)
        if "total" not in timings:
            timings["total"] = elapsed_ms

        tracking = self._tracking_index(symbol)
        references = [dict(item) for item in self._registry["market_references"]]
        return {
            "schema_version": "1.0",
            "symbol": symbol,
            "trade_date": target,
            "overall_status": overall_status,
            "core": core_blocks,
            "enrichment": {"etf_shares": shares},
            "tracking_index": tracking,
            "market_references": references,
            "retrieved_at": retrieved_at,
            "latency_ms": timings,
        }

    def _snapshot_block(self, raw: Any, target: str) -> dict[str, Any]:
        base = self._base_block(raw, requested_trade_date=None, source="tencent", units={
            "price": "CNY", "change_pct": "percent", "amount_wan": "CNY_10k"
        })
        quote = raw.get("quote") if isinstance(raw, dict) else None
        expected_latest = self._date(raw.get("expected_latest_trade_date")) if isinstance(raw, dict) else None
        if expected_latest is None or expected_latest < target:
            raise MarketDataSourceError()
        if target < expected_latest:
            if (
                base["status"] != "NOT_APPLICABLE"
                or quote is not None
                or base["source_trade_date"] is not None
                or base["as_of"] is not None
            ):
                raise MarketDataSourceError()
            base["data"] = None
            return base
        if base["status"] == "NOT_APPLICABLE":
            raise MarketDataSourceError()
        if base["status"] in {"OK", "PARTIAL"}:
            if not isinstance(quote, dict):
                raise MarketDataSourceError()
            name = quote.get("name")
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise MarketDataSourceError()
            data = {"name": name}
            for source_key, public_key in _QUOTE_FIELDS.items():
                if source_key not in quote:
                    if source_key == "amount_wan":
                        continue
                    raise MarketDataSourceError()
                data[public_key] = self._number(quote[source_key])
                if public_key in {"price", "last_close", "open", "high", "low"} and data[public_key] <= 0:
                    raise MarketDataSourceError()
                if public_key == "amount_wan" and data[public_key] < 0:
                    raise MarketDataSourceError()
            freshness = raw.get("freshness")
            if not isinstance(freshness, dict):
                raise MarketDataSourceError()
            freshness_out: dict[str, Any] = {}
            for key in ("quote_stale", "daily_kline_stale", "date_consistent", "source_is_stale"):
                value = freshness.get(key)
                if value is not None and type(value) is not bool:
                    raise MarketDataSourceError()
                freshness_out[key] = value
            quote_timestamp = self._timestamp_optional(quote.get("quote_timestamp"))
            quote_date = quote_timestamp[:10] if quote_timestamp is not None else None
            source_trade_date = self._date(base["source_trade_date"])
            if source_trade_date != quote_date:
                raise MarketDataSourceError()
            base["source_trade_date"] = source_trade_date
            base["as_of"] = quote_timestamp
            base["freshness_status"] = (
                "STALE" if freshness_out["quote_stale"] is True or freshness_out["source_is_stale"] is True
                else "FRESH" if quote_timestamp is not None and freshness_out["quote_stale"] is False
                else "UNKNOWN"
            )
            base["data"] = {"quote": data, "freshness": freshness_out, "expected_latest_trade_date": expected_latest}
        else:
            base["data"] = None
        return base

    def _intraday_block(self, raw: Any, target: str) -> dict[str, Any]:
        base = self._base_block(raw, requested_trade_date=target, source="tencent", units={
            "price": "CNY", "volume": "lots_100_shares"
        })
        raw_bars = raw.get("bars") if isinstance(raw, dict) else None
        if base["status"] in {"OK", "PARTIAL"}:
            bars = self._bars(raw_bars, kind="intraday", maximum=320)
            if any(bar["datetime"][:10] != target for bar in bars):
                raise MarketDataSourceError()
            if base["status"] == "OK" and not bars:
                raise MarketDataSourceError()
            base["data"] = bars
            if bars:
                if base["source_trade_date"] != target:
                    raise MarketDataSourceError()
                base["source_trade_date"] = target
                base["freshness_status"] = "FRESH"
            else:
                base["freshness_status"] = "UNKNOWN"
        else:
            base["data"] = None
        return base

    def _daily_block(self, raw: Any, target: str) -> dict[str, Any]:
        base = self._base_block(raw, requested_trade_date=target, source="tencent", units={
            "price": "CNY", "volume": "lots_100_shares"
        })
        raw_bars = raw.get("bars") if isinstance(raw, dict) else None
        if base["status"] in {"OK", "PARTIAL"}:
            bars = self._bars(raw_bars, kind="daily", maximum=60)
            if any(bar["date"] > target for bar in bars):
                raise MarketDataSourceError()
            if bars != sorted(bars, key=lambda bar: bar["date"]) or len({bar["date"] for bar in bars}) != len(bars):
                raise MarketDataSourceError()
            last_date = bars[-1]["date"] if bars else None
            if self._date(base["source_trade_date"]) != last_date:
                raise MarketDataSourceError()
            if base["status"] == "OK" and (len(bars) != 60 or last_date != target):
                raise MarketDataSourceError()
            if base["status"] == "PARTIAL" and not bars:
                raise MarketDataSourceError()
            base["data"] = bars
            base["freshness_status"] = "FRESH" if last_date == target else "STALE" if last_date else "UNKNOWN"
        else:
            base["data"] = None
        return base

    def _base_block(self, raw: Any, *, requested_trade_date: Optional[str], source: str, units: dict[str, str]) -> dict[str, Any]:
        if not isinstance(raw, dict) or raw.get("source") != source:
            raise MarketDataSourceError()
        status = raw.get("status")
        allowed_statuses = _STATUS | {"NOT_APPLICABLE"} if requested_trade_date is None else _STATUS
        if not isinstance(status, str) or status not in allowed_statuses:
            raise MarketDataSourceError()
        if requested_trade_date is not None and raw.get("requested_trade_date", requested_trade_date) != requested_trade_date:
            raise MarketDataSourceError()
        source_day = self._date(raw.get("source_trade_date"))
        as_of = self._timestamp_optional(raw.get("as_of"))
        freshness = raw.get("freshness_status", "UNKNOWN")
        if not isinstance(freshness, str) or freshness not in _FRESHNESS:
            raise MarketDataSourceError()
        evidence = raw.get("evidence_level", "LEVEL_A_RAW_FACT")
        if not isinstance(evidence, str) or evidence not in _EVIDENCE:
            raise MarketDataSourceError()
        warnings = raw.get("warnings", [])
        if not isinstance(warnings, list) or any(not isinstance(item, str) or item not in _WARNINGS for item in warnings):
            raise MarketDataSourceError()
        return {
            "status": status,
            "source": source,
            "requested_trade_date": requested_trade_date,
            "source_trade_date": source_day,
            "as_of": as_of,
            "freshness_status": freshness,
            "evidence_level": evidence,
            "units": units,
            "warnings": list(warnings),
            "data": None,
        }

    def _shares_block(
        self, raw: Any, symbol: str, target: str, previous: str
    ) -> tuple[dict[str, Any], dict[str, float]]:
        if not isinstance(raw, dict) or set(raw) != {"latest", "previous", "source"}:
            raise MarketDataSourceError()
        source = raw.get("source")
        if not isinstance(source, str) or source not in {"sse", "szse"}:
            raise MarketDataSourceError()
        latest = self._share_observation(raw.get("latest"), target)
        prior = self._share_observation(raw.get("previous"), previous)
        good_latest = latest["status"] == "OK"
        good_previous = prior["status"] == "OK"
        warnings: list[str] = []
        if not good_latest:
            warnings.append("LATEST_UNAVAILABLE")
        if not good_previous:
            warnings.append("PREVIOUS_UNAVAILABLE")
        if good_latest and good_previous:
            status = "OK"
            absolute = round(latest["shares_10k"] - prior["shares_10k"], 8)
            percent = (
                round(absolute / prior["shares_10k"] * 100, 8)
                if prior["shares_10k"] != 0
                else None
            )
            if percent is None:
                status = "PARTIAL"
                warnings.append("PREVIOUS_SHARES_ZERO")
        elif good_latest or good_previous:
            status = "PARTIAL"
            absolute = None
            percent = None
        elif latest["status"] == "TIMEOUT" or prior["status"] == "TIMEOUT":
            status = "TIMEOUT"
            absolute = None
            percent = None
        else:
            status = "UNAVAILABLE"
            absolute = None
            percent = None
        source_day = latest["source_trade_date"] if good_latest else prior["source_trade_date"] if good_previous else None
        as_of = latest["as_of"] if good_latest else prior["as_of"] if good_previous else None
        block = {
            "status": status,
            "source": source,
            "requested_trade_date": target,
            "source_trade_date": source_day,
            "as_of": as_of,
            "freshness_status": "FRESH" if good_latest else "PARTIAL" if good_previous else "UNKNOWN",
            "evidence_level": "LEVEL_A_RAW_FACT",
            "units": {"shares": "shares_10k", "change_pct": "percent"},
            "shares_latest": latest["shares_10k"],
            "shares_previous": prior["shares_10k"],
            "change_absolute": absolute,
            "change_pct": percent,
            "observations": {"latest": latest, "previous": prior},
            "warnings": warnings,
        }
        timings = {
            "shares_latest": latest["latency_ms"],
            "shares_previous": prior["latency_ms"],
        }
        return block, timings

    def _share_observation(self, raw: Any, requested: str) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise MarketDataSourceError()
        status = raw.get("status")
        if not isinstance(status, str) or status not in _STATUS:
            raise MarketDataSourceError()
        if raw.get("requested_trade_date") != requested:
            raise MarketDataSourceError()
        source_day = self._date(raw.get("source_trade_date"))
        as_of = self._timestamp_optional(raw.get("as_of"))
        value = raw.get("shares_10k")
        shares = self._number(value) if value is not None else None
        if shares is not None and shares < 0:
            raise MarketDataSourceError()
        if status == "OK" and (source_day != requested or shares is None):
            raise MarketDataSourceError()
        if status != "OK" and shares is not None:
            raise MarketDataSourceError()
        latency = self._number(raw.get("latency_ms"))
        if latency < 0:
            raise MarketDataSourceError()
        return {
            "status": status,
            "requested_trade_date": requested,
            "source_trade_date": source_day,
            "shares_10k": shares,
            "as_of": as_of,
            "latency_ms": latency,
        }

    def _tracking_index(self, symbol: str) -> dict[str, Any]:
        item = self._registry["instruments"].get(symbol)
        tracking = item.get("tracking_index") if isinstance(item, dict) else None
        if tracking is None:
            return {"code": None, "name": None, "identity_status": "UNAVAILABLE", "market_data_status": "UNAVAILABLE"}
        return {
            "code": tracking["code"],
            "name": tracking["name"],
            "identity_status": tracking["verification_status"],
            "market_data_status": "UNAVAILABLE",
        }

    @classmethod
    def _bars(cls, raw: Any, *, kind: str, maximum: int) -> list[dict[str, Any]]:
        if not isinstance(raw, list) or len(raw) > maximum:
            raise MarketDataSourceError()
        output: list[dict[str, Any]] = []
        previous = ""
        for row in raw:
            if not isinstance(row, dict):
                raise MarketDataSourceError()
            day_key = "datetime" if kind == "intraday" else "date"
            day = row.get(day_key)
            if not isinstance(day, str):
                raise MarketDataSourceError()
            if kind == "intraday":
                try:
                    parsed = datetime.strptime(day, "%Y-%m-%d %H:%M")
                except ValueError:
                    raise MarketDataSourceError() from None
                if parsed.strftime("%Y-%m-%d %H:%M") != day or day <= previous:
                    raise MarketDataSourceError()
            else:
                normalized = cls._date(day)
                if normalized is None or normalized <= previous:
                    raise MarketDataSourceError()
            item: dict[str, Any] = {day_key: day}
            for field in _BAR_FIELDS:
                if field not in row:
                    raise MarketDataSourceError()
                item[field] = cls._number(row[field])
            if item["volume"] < 0:
                raise MarketDataSourceError()
            if any(item[field] <= 0 for field in ("open", "high", "low", "close")):
                raise MarketDataSourceError()
            if item["high"] < item["low"]:
                raise MarketDataSourceError()
            output.append(item)
            previous = day
        return output

    @staticmethod
    def _normalize_timings(raw: Any) -> dict[str, float]:
        if not isinstance(raw, dict):
            return {}
        fields = ("adapter_setup", "calendar", "quote", "intraday_5m", "daily", "shares", "total")
        result: dict[str, float] = {}
        for key in fields:
            if key in raw:
                value = MarketContextService._number(raw[key])
                if value < 0:
                    raise MarketDataSourceError()
                result[key] = value
        return result

    @staticmethod
    def _number(value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise MarketDataSourceError()
        number = float(value)
        if not math.isfinite(number):
            raise MarketDataSourceError()
        return number

    @staticmethod
    def _date(value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError:
            return None
        return normalized if normalized == value else None

    @staticmethod
    def _timestamp(value: Any) -> str:
        parsed = MarketContextService._timestamp_optional(value)
        if parsed is None:
            raise MarketDataSourceError()
        return parsed

    @staticmethod
    def _timestamp_optional(value: Any) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str):
            raise MarketDataSourceError()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise MarketDataSourceError() from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise MarketDataSourceError()
        return parsed.isoformat()

    def _elapsed_ms(self, started: float) -> float:
        return round(max(0.0, (self._clock() - started) * 1000), 3)
