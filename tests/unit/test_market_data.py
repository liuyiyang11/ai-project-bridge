from __future__ import annotations

import json
import subprocess
import calendar
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from bridge.config import BridgeConfig, MarketDataConfig, load_config
from bridge.market_data import adapter as market_adapter
from bridge.market_data.adapter import _resolve_market_context
from bridge.market_data.service import MarketDataService
from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools, McpToolError


_SECRET_PATH = r"F:\private\secret.py"
_SECRET_TOKEN = "token=sk-test-secret"


def _config(tmp_path: Path, *, timeout: int = 30) -> MarketDataConfig:
    root = tmp_path / "a-stock-data"
    root.mkdir(exist_ok=True)
    (root / "SKILL.md").write_text("test", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"test")
    return MarketDataConfig(
        enabled=True,
        root=root,
        python_executable=python,
        default_timeout_seconds=timeout,
    )


def _payload(symbol: str = "600000", *, days: int = 1, adjust: str = "qfq") -> dict:
    return {
        "symbol": symbol,
        "name": "浦发银行",
        "adjust": adjust,
        "quote": {
            "name": "浦发银行",
            "price": 8.98,
            "last_close": 9.04,
            "open": 9.02,
            "high": 9.05,
            "low": 8.95,
            "change_amt": -0.06,
            "change_pct": -0.66,
            "amount_wan": 45895.0,
        },
        "daily_kline": [
            {
                "date": "2026-09-23",
                "open": 9.02,
                "high": 9.05,
                "low": 8.95,
                "close": 8.98,
                "volume": 511244,
            }
        ][:days],
        "data_source": {"quote": "tencent", "daily_kline": "tencent"},
        "retrieved_at": "2026-09-23T08:00:00+00:00",
        "quote_timestamp": None,
        "market_date": "2026-09-23",
        "expected_latest_trade_date": "2026-09-23",
        "market_status": "CLOSED",
        "_timings_ms": {"adapter_setup": 42.0, "quote": 80.0, "daily_kline": 110.0},
        "warnings": [],
    }


class _ForbiddenSupervisor:
    def __getattr__(self, name):
        raise AssertionError(f"market snapshot must not access supervisor method {name}")


def _tool(service: MarketDataService) -> BridgeMcpTools:
    return BridgeMcpTools(supervisor=_ForbiddenSupervisor(), market_data_service=service)


def _mcp_call(service: MarketDataService, arguments: dict) -> dict:
    return McpStdioServer(_tool(service)).handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "bridge_market_snapshot", "arguments": arguments},
        }
    )["result"]


def _snapshot_from_payload(tmp_path: Path, payload: dict) -> dict:
    service = MarketDataService(
        _config(tmp_path),
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(payload, ensure_ascii=False), ""
        ),
    )
    return service.snapshot("600000", 20, "qfq")


def _calendar_rows(year: int, month: int, open_days: set[str]) -> list[dict]:
    last_day = calendar.monthrange(year, month)[1]
    return [
        {
            "date": date(year, month, day).isoformat(),
            "is_open": date(year, month, day).isoformat() in open_days,
        }
        for day in range(1, last_day + 1)
    ]


def _calendar_lookup(calendars: dict[tuple[int, int], set[str]]):
    def lookup(year: int, month: int):
        return _calendar_rows(year, month, calendars[(year, month)])

    return lookup


def test_market_snapshot_one_explicit_bounded_subprocess_and_public_shape(tmp_path):
    payload = _payload(adjust="none")
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload, ensure_ascii=False), "")

    config = _config(tmp_path)
    service = MarketDataService(config, runner=runner)
    result = service.snapshot("600000", 20, "none")

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert isinstance(argv, list)
    assert argv[0].endswith("python.exe")
    assert argv[argv.index("--symbol") + 1] == "600000"
    assert argv[argv.index("--days") + 1] == "20"
    assert argv[argv.index("--adjust") + 1] == "none"
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 30
    assert kwargs["encoding"] == "utf-8"
    assert str(config.root) not in json.dumps(result)
    assert result["quote"]["change"] == -0.06
    assert result["quote"]["amount_wan"] == 45895.0
    assert result["daily_kline"][0]["close"] == 8.98
    assert result["adjust"] == "none"
    assert result["data_source"] == {"quote": "tencent", "daily_kline": "tencent"}
    assert set(result) == {
        "symbol", "name", "quote", "daily_kline", "latest_price_bar", "data_quality",
        "adjust", "data_source", "retrieved_at",
        "market_date", "quote_timestamp", "daily_kline_last_date", "expected_latest_trade_date",
        "market_status", "data_freshness", "latency_ms", "warnings",
    }
    assert result["market_date"] == "2026-09-23"
    assert result["daily_kline_last_date"] == "2026-09-23"
    assert result["data_freshness"] == {
        "quote_stale": None,
        "daily_kline_stale": False,
        "date_consistent": None,
    }
    assert result["latest_price_bar"] == {
        **payload["daily_kline"][0],
        "adjust": "none", "role": "EXACT_LATEST_PRICE_REFERENCE", "source": "tencent",
    }
    assert result["data_quality"]["latest_daily_bar"]["status"] == "CONSISTENT"
    assert result["data_quality"]["latest_daily_bar"]["exact_price_safe"] is True


def _quality_payload(*, adjust: str = "qfq") -> dict:
    payload = _payload("560780", adjust=adjust)
    prior = {"date": "2026-09-24", "open": 1.05, "high": 1.058,
             "low": 1.032, "close": 1.032, "volume": 3656727.0}
    latest = {"date": "2026-09-28", "open": 1.022, "high": 1.038,
              "low": 0.984, "close": 0.988, "volume": 5846140.0}
    payload["daily_kline"] = [prior, dict(latest)]
    payload["_none_reference"] = {"source": "tencent", "adjust": "none", "bar": dict(latest)}
    payload["retrieved_at"] = "2026-09-28T08:10:00+00:00"
    payload["market_date"] = "2026-09-28"
    payload["expected_latest_trade_date"] = "2026-09-28"
    return payload


def _quality_snapshot(tmp_path: Path, payload: dict) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    service = MarketDataService(
        _config(tmp_path),
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(payload, ensure_ascii=False), ""
        ),
    )
    return service.snapshot(payload["symbol"], 20, payload["adjust"])


def test_qfq_matching_reference_marks_latest_price_safe(tmp_path):
    result = _quality_snapshot(tmp_path, _quality_payload())

    quality = result["data_quality"]["latest_daily_bar"]
    assert quality == {
        "status": "CONSISTENT", "exact_price_safe": True, "freshness_status": "VERIFIED",
        "qfq_date": "2026-09-28", "reference_date": "2026-09-28",
        "max_abs_diff": 0.0, "fields_different": [],
    }
    assert result["latest_price_bar"]["adjust"] == "none"
    assert result["latest_price_bar"]["close"] == 0.988
    assert result["warnings"] == []


def test_qfq_inconsistent_reference_preserves_source_series_and_warns(tmp_path):
    payload = _quality_payload()
    payload["daily_kline"][-1].update(open=1.02, high=1.04, low=0.98, close=0.99)

    result = _quality_snapshot(tmp_path, payload)

    quality = result["data_quality"]["latest_daily_bar"]
    assert result["daily_kline"] == payload["daily_kline"]
    assert result["latest_price_bar"] == {
        **payload["_none_reference"]["bar"],
        "adjust": "none", "role": "EXACT_LATEST_PRICE_REFERENCE", "source": "tencent",
    }
    assert quality["status"] == "INCONSISTENT"
    assert quality["exact_price_safe"] is False
    assert quality["max_abs_diff"] == 0.004
    assert quality["fields_different"] == ["open", "high", "low", "close"]
    assert len(result["warnings"]) == 1
    assert "inconsistent" in result["warnings"][0]


def test_qfq_one_observed_tick_is_detected(tmp_path):
    payload = _quality_payload()
    payload["daily_kline"][-1]["low"] = 0.985

    quality = _quality_snapshot(tmp_path, payload)["data_quality"]["latest_daily_bar"]

    assert quality["status"] == "INCONSISTENT"
    assert quality["fields_different"] == ["low"]
    assert quality["max_abs_diff"] == 0.001


def test_qfq_missing_or_misaligned_reference_is_unverified(tmp_path):
    missing = _quality_payload()
    missing["_none_reference"] = None
    result = _quality_snapshot(tmp_path / "missing", missing)
    quality = result["data_quality"]["latest_daily_bar"]
    assert quality["status"] == "UNVERIFIED"
    assert quality["exact_price_safe"] is None
    assert result["latest_price_bar"] is None

    mismatched = _quality_payload()
    mismatched["_none_reference"]["bar"]["date"] = "2026-09-24"
    result = _quality_snapshot(tmp_path / "mismatched", mismatched)
    quality = result["data_quality"]["latest_daily_bar"]
    assert quality["status"] == "UNVERIFIED"
    assert quality["reference_date"] == "2026-09-24"
    assert quality["max_abs_diff"] is None
    assert result["latest_price_bar"] is None


def test_qfq_calendar_unavailable_preserves_price_result_but_unknown_freshness(tmp_path):
    payload = _quality_payload()
    payload["expected_latest_trade_date"] = None
    payload["market_status"] = "UNKNOWN"

    result = _quality_snapshot(tmp_path, payload)

    quality = result["data_quality"]["latest_daily_bar"]
    assert quality["status"] == "CONSISTENT"
    assert quality["freshness_status"] == "UNKNOWN"
    assert quality["exact_price_safe"] is None
    assert result["latest_price_bar"]["close"] == 0.988


def test_qfq_stale_date_is_not_compared_even_with_same_date_reference(tmp_path):
    payload = _quality_payload()
    payload["retrieved_at"] = "2026-09-29T08:10:00+00:00"
    payload["market_date"] = "2026-09-29"
    payload["expected_latest_trade_date"] = "2026-09-29"

    result = _quality_snapshot(tmp_path, payload)

    quality = result["data_quality"]["latest_daily_bar"]
    assert quality["status"] == "UNVERIFIED"
    assert quality["freshness_status"] == "MISMATCH"
    assert quality["exact_price_safe"] is False
    assert quality["max_abs_diff"] is None
    assert result["latest_price_bar"] is None


def test_none_and_hfq_do_not_apply_qfq_comparison(tmp_path):
    for adjust in ("none", "hfq"):
        payload = _quality_payload(adjust=adjust)
        payload["_none_reference"]["bar"]["low"] = 0.1
        result = _quality_snapshot(tmp_path / adjust, payload)
        quality = result["data_quality"]["latest_daily_bar"]
        assert quality["fields_different"] == []
        assert quality["max_abs_diff"] is None
        assert result["warnings"] == []
        if adjust == "none":
            assert quality["status"] == "CONSISTENT"
            assert quality["exact_price_safe"] is True
            assert result["latest_price_bar"]["low"] == 0.984
        else:
            assert quality["status"] == "UNVERIFIED"
            assert quality["exact_price_safe"] is False
            assert result["latest_price_bar"] is None


def test_malicious_optional_reference_is_not_forwarded(tmp_path):
    payload = _quality_payload()
    payload["_none_reference"]["bar"].update(
        low=_SECRET_TOKEN, cookie="session-cookie-secret", path=_SECRET_PATH,
    )
    result = _quality_snapshot(tmp_path, payload)
    public = json.dumps(result, ensure_ascii=False)

    assert result["data_quality"]["latest_daily_bar"]["status"] == "UNVERIFIED"
    assert result["latest_price_bar"] is None
    for secret in (_SECRET_TOKEN, "session-cookie-secret", _SECRET_PATH):
        assert secret not in public


def _fake_adapter_namespace(calls: list, *, reference_timeout: bool = False) -> dict:
    source = _quality_payload()
    namespace = {"norm_ticker": lambda symbol, stock_only: symbol, "_v39_http": lambda *a, **k: None}

    def kline(code, *, period, adjust, count):
        calls.append((code, period, adjust, count))
        if adjust == "" and reference_timeout:
            raise TimeoutError(f"private reference error {_SECRET_TOKEN}")
        row = source["_none_reference"]["bar"] if adjust == "" else source["daily_kline"][-1]
        return pd.DataFrame([{**row, "source": "tencent", "source_url": _SECRET_PATH}])

    namespace["tencent_kline"] = kline
    namespace["tencent_quote"] = lambda codes: {codes[0]: {**source["quote"], "is_stale": False}}
    return namespace


def test_optional_reference_timeout_does_not_fail_adapter_or_snapshot(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(market_adapter, "_load_functions", lambda root: _fake_adapter_namespace(
        calls, reference_timeout=True
    ))
    monkeypatch.setattr(market_adapter, "_load_trading_calendar", lambda root: (_ for _ in ()).throw(
        RuntimeError("calendar unavailable")
    ))

    payload = market_adapter.run(Namespace(repo_root=tmp_path, symbol="560780", days=20, adjust="qfq"))
    result = MarketDataService(_config(tmp_path))._normalize(
        payload, symbol="560780", days=20, adjust="qfq"
    )

    assert len(calls) == 2
    assert set(calls) == {("560780", "day", "qfq", 20), ("560780", "day", "", 2)}
    assert payload["_none_reference"] is None
    assert result["daily_kline"][-1]["close"] == 0.988
    assert result["data_quality"]["latest_daily_bar"]["status"] == "UNVERIFIED"
    assert _SECRET_TOKEN not in json.dumps(result)


def test_optional_reference_success_is_allowlisted_through_adapter(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(market_adapter, "_load_functions", lambda root: _fake_adapter_namespace(calls))
    monkeypatch.setattr(market_adapter, "_load_trading_calendar", lambda root: (_ for _ in ()).throw(
        RuntimeError("calendar unavailable")
    ))

    payload = market_adapter.run(Namespace(repo_root=tmp_path, symbol="560780", days=20, adjust="qfq"))
    result = MarketDataService(_config(tmp_path))._normalize(
        payload, symbol="560780", days=20, adjust="qfq"
    )

    assert len(calls) == 2
    assert set(calls) == {("560780", "day", "qfq", 20), ("560780", "day", "", 2)}
    assert payload["_none_reference"]["bar"]["close"] == 0.988
    assert result["data_quality"]["latest_daily_bar"]["status"] == "CONSISTENT"
    assert result["data_quality"]["latest_daily_bar"]["freshness_status"] == "UNKNOWN"
    assert result["latest_price_bar"]["close"] == 0.988
    assert _SECRET_PATH not in json.dumps(result)


@pytest.mark.parametrize("adjust,source_adjust", [("none", ""), ("hfq", "hfq")])
def test_adapter_skips_redundant_none_reference_for_non_qfq(tmp_path, monkeypatch, adjust, source_adjust):
    calls = []
    monkeypatch.setattr(market_adapter, "_load_functions", lambda root: _fake_adapter_namespace(calls))
    monkeypatch.setattr(market_adapter, "_load_trading_calendar", lambda root: (_ for _ in ()).throw(
        RuntimeError("calendar unavailable")
    ))

    payload = market_adapter.run(Namespace(repo_root=tmp_path, symbol="560780", days=20, adjust=adjust))

    assert calls == [("560780", "day", source_adjust, 20)]
    assert payload["_none_reference"] is None


def test_market_context_uses_official_calendar_for_normal_trading_session():
    now = datetime(2026, 9, 28, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    calendar_lookup = _calendar_lookup({
        (2026, 9): {"2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"},
    })

    context = _resolve_market_context(now, calendar_lookup)

    assert context == {
        "market_date": "2026-09-28",
        "expected_latest_trade_date": "2026-09-24",
        "market_status": "OPEN",
    }


def test_market_snapshot_normal_trading_day_quote_and_kline_are_consistent(tmp_path):
    now = datetime(2026, 9, 28, 16, 5, tzinfo=timezone(timedelta(hours=8)))
    context = _resolve_market_context(now, _calendar_lookup({
        (2026, 9): {"2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"},
    }))
    payload = _payload()
    payload["retrieved_at"] = now.astimezone(timezone.utc).isoformat()
    payload["quote_timestamp"] = "2026-09-28T08:04:30+00:00"
    payload["daily_kline"][0]["date"] = "2026-09-28"
    payload.update(context)

    result = _snapshot_from_payload(tmp_path, payload)

    assert result["market_date"] == "2026-09-28"
    assert result["expected_latest_trade_date"] == "2026-09-28"
    assert result["market_status"] == "CLOSED"
    assert result["data_freshness"] == {
        "quote_stale": False,
        "daily_kline_stale": False,
        "date_consistent": True,
    }
    assert result["warnings"] == []


def test_market_snapshot_weekend_friday_kline_is_not_stale(tmp_path):
    now = datetime(2026, 10, 10, 11, 0, tzinfo=timezone(timedelta(hours=8)))
    context = _resolve_market_context(now, _calendar_lookup({
        (2026, 10): {"2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"},
    }))
    payload = _payload()
    payload["retrieved_at"] = now.astimezone(timezone.utc).isoformat()
    payload["quote_timestamp"] = "2026-10-09T07:00:00+00:00"
    payload["daily_kline"][0]["date"] = "2026-10-09"
    payload.update(context)

    result = _snapshot_from_payload(tmp_path, payload)

    assert result["market_status"] == "CLOSED"
    assert result["expected_latest_trade_date"] == "2026-10-09"
    assert result["data_freshness"]["daily_kline_stale"] is False
    assert result["warnings"] == []


def test_market_snapshot_statutory_holiday_does_not_report_stale(tmp_path):
    now = datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=8)))
    context = _resolve_market_context(now, _calendar_lookup({
        (2026, 10): {"2026-10-08", "2026-10-09", "2026-10-12"},
        (2026, 9): {"2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"},
    }))
    payload = _payload()
    payload["retrieved_at"] = now.astimezone(timezone.utc).isoformat()
    payload["quote_timestamp"] = "2026-09-30T07:00:00+00:00"
    payload["daily_kline"][0]["date"] = "2026-09-30"
    payload.update(context)

    result = _snapshot_from_payload(tmp_path, payload)

    assert result["market_status"] == "CLOSED"
    assert result["expected_latest_trade_date"] == "2026-09-30"
    assert result["data_freshness"]["daily_kline_stale"] is False
    assert result["warnings"] == []


def test_market_snapshot_reports_kline_one_expected_trade_date_behind(tmp_path):
    now = datetime(2026, 9, 28, 16, 0, tzinfo=timezone(timedelta(hours=8)))
    context = _resolve_market_context(now, _calendar_lookup({
        (2026, 9): {"2026-09-24", "2026-09-28", "2026-09-29", "2026-09-30"},
    }))
    payload = _payload()
    payload["retrieved_at"] = now.astimezone(timezone.utc).isoformat()
    payload["daily_kline"][0]["date"] = "2026-09-24"
    payload.update(context)

    result = _snapshot_from_payload(tmp_path, payload)

    assert result["expected_latest_trade_date"] == "2026-09-28"
    assert result["data_freshness"]["daily_kline_stale"] is True
    assert "daily kline is behind the latest expected trading date" in result["warnings"]


def test_market_snapshot_unknown_quote_timestamp_is_not_guessed(tmp_path):
    result = _snapshot_from_payload(tmp_path, _payload())

    assert result["quote_timestamp"] is None
    assert result["data_freshness"]["quote_stale"] is None
    assert result["data_freshness"]["date_consistent"] is None


def test_market_calendar_failure_degrades_to_unknown_without_warning(tmp_path):
    now = datetime(2026, 9, 28, 16, 0, tzinfo=timezone(timedelta(hours=8)))

    def failed_calendar(year: int, month: int):
        raise RuntimeError("calendar source failure with private detail")

    context = _resolve_market_context(now, failed_calendar)
    payload = _payload()
    payload["retrieved_at"] = now.astimezone(timezone.utc).isoformat()
    payload["daily_kline"][0]["date"] = "2026-09-24"
    payload.update(context)
    result = _snapshot_from_payload(tmp_path, payload)
    public = json.dumps(result, ensure_ascii=False)

    assert context["expected_latest_trade_date"] is None
    assert context["market_status"] == "UNKNOWN"
    assert result["data_freshness"]["daily_kline_stale"] is None
    assert result["warnings"] == []
    assert "private detail" not in public


@pytest.mark.parametrize(
    "symbol",
    ["../../secret", "600000 && whoami", "abcdef", "", "6" * 256],
)
def test_invalid_symbols_are_rejected_before_subprocess(tmp_path, symbol):
    calls = []
    service = MarketDataService(_config(tmp_path), runner=lambda *args, **kwargs: calls.append(args))

    with pytest.raises(McpToolError):
        _tool(service).call("bridge_market_snapshot", {"symbol": symbol})

    assert calls == []


@pytest.mark.parametrize("days", [0, 251])
def test_days_outside_limits_are_rejected_before_subprocess(tmp_path, days):
    calls = []
    service = MarketDataService(_config(tmp_path), runner=lambda *args, **kwargs: calls.append(args))

    with pytest.raises(McpToolError):
        _tool(service).call("bridge_market_snapshot", {"symbol": "600000", "days": days})

    assert calls == []


@pytest.mark.parametrize("days", [1, 20, 250])
def test_days_boundaries_are_accepted(tmp_path, days):
    payload = _payload()
    calls = []
    service = MarketDataService(
        _config(tmp_path),
        runner=lambda argv, **kwargs: calls.append(argv)
        or subprocess.CompletedProcess(argv, 0, json.dumps(payload, ensure_ascii=False), ""),
    )

    result = _tool(service).call("bridge_market_snapshot", {"symbol": "600000", "days": days})

    assert result["daily_kline"]
    assert len(calls) == 1


def test_market_snapshot_timeout_has_safe_structured_public_error(tmp_path):
    def runner(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=_SECRET_PATH, stderr=f"{_SECRET_TOKEN}\nTraceback (most recent call last):")

    result = _mcp_call(MarketDataService(_config(tmp_path), runner=runner), {"symbol": "600000"})
    public = json.dumps(result, ensure_ascii=False)

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "error_code": "MARKET_DATA_TIMEOUT",
        "message": "Market data request timed out.",
    }
    assert _SECRET_PATH not in public
    assert "sk-test-secret" not in public
    assert "Traceback" not in public
    assert "argv" not in public


def test_market_snapshot_nonzero_exit_drops_malicious_stderr(tmp_path):
    def runner(argv, **kwargs):
        stderr = f"{_SECRET_PATH}\n{_SECRET_TOKEN}\nTraceback (most recent call last): secret"
        return subprocess.CompletedProcess(argv, 9, "", stderr)

    result = _mcp_call(MarketDataService(_config(tmp_path), runner=runner), {"symbol": "600000"})
    public = json.dumps(result, ensure_ascii=False)

    assert result["structuredContent"] == {
        "error_code": "MARKET_DATA_SOURCE_FAILED",
        "message": "Market data source is temporarily unavailable.",
    }
    for unsafe in (_SECRET_PATH, "sk-test-secret", "Traceback", "python.exe", "argv"):
        assert unsafe not in public


def test_market_snapshot_invalid_json_is_a_safe_source_failure(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, f"not json {_SECRET_PATH} {_SECRET_TOKEN}", "")

    result = _mcp_call(MarketDataService(_config(tmp_path), runner=runner), {"symbol": "600000"})
    public = json.dumps(result, ensure_ascii=False)

    assert result["structuredContent"]["error_code"] == "MARKET_DATA_SOURCE_FAILED"
    assert _SECRET_PATH not in public
    assert "sk-test-secret" not in public


def test_nested_adapter_fields_are_not_forwarded(tmp_path):
    payload = _payload()
    payload["quote"]["credential"] = _SECRET_TOKEN
    payload["quote"]["nested"] = {"path": _SECRET_PATH, "authorization": "Bearer sk-test-secret"}
    payload["daily_kline"][0]["cookie"] = "session-cookie-secret"
    payload["daily_kline"][0]["extra"] = {"traceback": "Traceback (most recent call last): local"}
    payload["warnings"] = [f"{_SECRET_PATH} {_SECRET_TOKEN}"]
    payload["quote_timestamp"] = _SECRET_TOKEN
    payload["market_date"] = _SECRET_PATH
    payload["expected_latest_trade_date"] = _SECRET_TOKEN
    payload["market_status"] = {"token": _SECRET_TOKEN}
    payload["_timings_ms"]["token"] = _SECRET_TOKEN
    payload["unknown"] = {"python_executable": r"F:\private\python.exe"}

    service = MarketDataService(
        _config(tmp_path),
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(payload, ensure_ascii=False), ""),
    )
    result = service.snapshot("600000")
    public = json.dumps(result, ensure_ascii=False)

    assert result["warnings"] == []
    for unsafe in (_SECRET_PATH, "sk-test-secret", "session-cookie-secret", "Traceback", "python.exe", "credential", "cookie", "authorization"):
        assert unsafe not in public


def test_market_snapshot_rejects_bad_name_and_non_tencent_source(tmp_path):
    for mutate in (
        lambda payload: payload.update(name=_SECRET_PATH),
        lambda payload: payload.update(data_source={"quote": "other", "daily_kline": "tencent"}),
    ):
        payload = _payload()
        mutate(payload)
        service = MarketDataService(
            _config(tmp_path),
            runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(payload), ""),
        )
        with pytest.raises(McpToolError, match="temporarily unavailable"):
            _tool(service).call("bridge_market_snapshot", {"symbol": "600000"})


def test_tools_list_has_market_fast_path_and_preserves_original_nine():
    response = McpStdioServer(BridgeMcpTools(supervisor=_ForbiddenSupervisor())).handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    tools = response["result"]["tools"]
    names = {tool["name"] for tool in tools}

    assert len(tools) == 11
    assert names == set(BridgeMcpTools._SCHEMAS)
    definition = next(tool for tool in tools if tool["name"] == "bridge_market_snapshot")
    assert "synchronous" in definition["description"]
    assert "does not start a Codex task" in definition["description"]
    assert "investment advice" in definition["description"]


def test_market_data_config_requires_local_paths_when_enabled(tmp_path):
    with pytest.raises(ValueError, match="requires root and python_executable"):
        MarketDataConfig(enabled=True)

    config_path = tmp_path / "config.yaml"
    root = tmp_path / "repo"
    python = tmp_path / "python.exe"
    root.mkdir()
    python.write_bytes(b"python")
    config_path.write_text(
        f"""control_repo: owner/bridge
trusted_github_logins: [trusted-user]
projects: {{}}
market_data:
  enabled: true
  root: '{root.as_posix()}'
  python_executable: '{python.as_posix()}'
  default_timeout_seconds: 30
""",
        encoding="utf-8",
    )

    loaded = load_config(config_path)
    assert loaded.market_data is not None
    assert loaded.market_data.enabled is True
    assert loaded.market_data.root == root.resolve()
    assert loaded.market_data.python_executable == python.resolve()
