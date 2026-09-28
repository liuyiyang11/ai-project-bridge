from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta, timezone
import json
import subprocess

import pytest

import bridge.market_data.context_adapter as context_adapter
from bridge.market_data.context import MarketContextService
from bridge.market_data.context_adapter import (
    InvalidTradeDate,
    _intraday_block,
    _request_local_calendar,
    _resolve_trade_dates,
    run as run_context_adapter,
)
from bridge.market_data.errors import MarketDataRequestError, MarketDataSourceError, MarketDataTimeoutError
from bridge.mcp.server import McpStdioServer
from bridge.mcp.tools import BridgeMcpTools
from bridge.market_data.service import MarketDataService
from bridge.config import MarketDataConfig


TARGET = "2026-09-25"
PREVIOUS = "2026-09-24"
_SECRET_PATH = r"F:\private\secret.py"
_SECRET_TOKEN = "token=sk-test-secret"


def _config(tmp_path, timeout: int = 12) -> MarketDataConfig:
    root = tmp_path / "a-stock-data"
    root.mkdir(exist_ok=True)
    (root / "SKILL.md").write_text("test", encoding="utf-8")
    python = tmp_path / "python.exe"
    python.write_bytes(b"test")
    return MarketDataConfig(
        enabled=True,
        root=root,
        python_executable=python,
        default_timeout_seconds=30,
        market_context_timeout_seconds=timeout,
    )


def _weekdays(end: str, count: int = 60) -> list[str]:
    current = date.fromisoformat(end)
    result = []
    while len(result) < count:
        if current.weekday() < 5:
            result.append(current.isoformat())
        current -= timedelta(days=1)
    return sorted(result)


def _bar(day: str) -> dict:
    return {"date": day, "open": 1.0, "high": 1.2, "low": 0.9, "close": 1.1, "volume": 1000}


def _payload(*, shares_latest: dict | None = None, shares_previous: dict | None = None) -> dict:
    dates = _weekdays(TARGET)
    bars = [_bar(day) for day in dates]
    return {
        "symbol": "560780",
        "trade_date": TARGET,
        "previous_trade_date": PREVIOUS,
        "retrieved_at": "2026-09-27T04:00:00+00:00",
        "core": {
            "snapshot": {
                "status": "OK",
                "source": "tencent",
                "source_trade_date": None,
                "as_of": None,
                "expected_latest_trade_date": TARGET,
                "quote": {
                    "name": "半导体材料ETF",
                    "price": 1.1,
                    "last_close": 1.08,
                    "open": 1.09,
                    "high": 1.12,
                    "low": 1.07,
                    "change_amt": 0.02,
                    "change_pct": 1.85,
                    "amount_wan": 1024.0,
                },
                "freshness": {
                    "quote_stale": None,
                    "daily_kline_stale": False,
                    "date_consistent": None,
                    "source_is_stale": False,
                },
                "warnings": [],
            },
            "intraday_5m": {
                "status": "OK",
                "source": "tencent",
                "requested_trade_date": TARGET,
                "source_trade_date": TARGET,
                "bars": [
                    {
                        "datetime": f"{TARGET} 09:35",
                        "open": 1.09,
                        "high": 1.1,
                        "low": 1.08,
                        "close": 1.1,
                        "volume": 100,
                    }
                ],
                "warnings": [],
            },
            "daily": {
                "status": "OK",
                "source": "tencent",
                "requested_trade_date": TARGET,
                "source_trade_date": TARGET,
                "bars": bars,
                "warnings": [],
            },
        },
        "etf_shares": {
            "source": "sse",
            "latest": shares_latest or {
                "status": "OK", "requested_trade_date": TARGET, "source_trade_date": TARGET,
                "shares_10k": 123456.0, "as_of": "2026-09-25T11:00:00+08:00", "latency_ms": 1200.0,
            },
            "previous": shares_previous or {
                "status": "OK", "requested_trade_date": PREVIOUS, "source_trade_date": PREVIOUS,
                "shares_10k": 120000.0, "as_of": "2026-09-24T11:00:00+08:00", "latency_ms": 1000.0,
            },
        },
        "_timings_ms": {
            "adapter_setup": 100.0,
            "calendar": 80.0,
            "quote": 200.0,
            "intraday_5m": 210.0,
            "daily": 220.0,
            "shares": 1200.0,
            "total": 1500.0,
        },
    }


class _ForbiddenSupervisor:
    def __getattr__(self, name):
        raise AssertionError(f"market context must not access supervisor method {name}")


def _service(tmp_path, payload: dict | None = None, runner=None) -> MarketContextService:
    if runner is None:
        runner = lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(payload or _payload(), ensure_ascii=False, separators=(",", ":")), ""
        )
    return MarketContextService(_config(tmp_path), runner=runner)


def _mcp_result(service: MarketContextService, arguments: dict) -> dict:
    tools = BridgeMcpTools(supervisor=_ForbiddenSupervisor(), market_context_service=service)
    return McpStdioServer(tools).handle_message({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "bridge_market_context", "arguments": arguments},
    })["result"]


def _calendar_rows(year: int, month: int, open_days: set[str]) -> list[dict]:
    last_day = calendar.monthrange(year, month)[1]
    return [
        {"date": date(year, month, day).isoformat(), "is_open": date(year, month, day).isoformat() in open_days}
        for day in range(1, last_day + 1)
    ]


def _calendar_lookup(open_by_month: dict[tuple[int, int], set[str]]):
    return lambda year, month: _calendar_rows(year, month, open_by_month[(year, month)])


class _AdapterSeries:
    def dropna(self):
        return self

    def unique(self):
        return ["tencent"]


class _AdapterFrame:
    def __init__(self, columns, rows):
        self.columns = set(columns)
        self.rows = rows

    def __getitem__(self, _key):
        return _AdapterSeries()

    def to_dict(self, orient):
        assert orient == "records"
        return self.rows


def _adapter_namespace(target: str, quote_calls: list[str]):
    def tencent_quote(codes):
        quote_calls.extend(codes)
        return {
            code: {
                "name": "半导体材料ETF",
                "price": 1.1,
                "last_close": 1.08,
                "open": 1.09,
                "high": 1.12,
                "low": 1.07,
                "change_amt": 0.02,
                "change_pct": 1.85,
                "amount_wan": 1024.0,
                "quote_timestamp": "2026-09-28T15:00:00+08:00",
                "is_stale": False,
            }
            for code in codes
        }

    def tencent_kline(_code, *, period, **_kwargs):
        if period == "m5":
            rows = [{
                "datetime": f"{target} 09:35",
                "open": 1.09,
                "high": 1.1,
                "low": 1.08,
                "close": 1.1,
                "volume": 100,
                "source": "tencent",
            }]
            columns = {"datetime", "open", "high", "low", "close", "volume", "source"}
        else:
            rows = [{**_bar(day), "source": "tencent"} for day in _weekdays(target)]
            columns = {"date", "open", "high", "low", "close", "volume", "source"}
        return _AdapterFrame(columns, rows)

    return {
        "norm_ticker": lambda symbol, stock_only=False: symbol,
        "tencent_quote": tencent_quote,
        "tencent_kline": tencent_kline,
        "_v39_http": lambda *args, **kwargs: None,
    }


def test_market_context_normal_etf_contract_and_two_day_shares(tmp_path):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(_payload(), ensure_ascii=False), "")

    result = _service(tmp_path, runner=runner).market_context("560780")

    assert result["overall_status"] == "OK"
    assert result["trade_date"] == TARGET
    assert len(result["core"]["daily"]["data"]) == 60
    assert result["core"]["intraday_5m"]["data"][0]["datetime"].startswith(TARGET)
    assert result["enrichment"]["etf_shares"]["status"] == "OK"
    shares = result["enrichment"]["etf_shares"]
    assert shares["units"]["shares"] == "shares_10k"
    assert shares["shares_latest"] == 123456.0
    assert shares["shares_previous"] == 120000.0
    assert shares["change_absolute"] == 3456.0
    assert round(shares["change_pct"], 6) == round(3456 / 120000 * 100, 6)
    assert result["tracking_index"] == {
        "code": "931743",
        "name": "中证半导体材料设备主题指数",
        "identity_status": "VERIFIED",
        "market_data_status": "UNAVAILABLE",
    }
    assert result["market_references"] == []
    assert result["core"]["snapshot"]["freshness_status"] == "UNKNOWN"
    assert len(calls) == 1
    argv, options = calls[0]
    assert argv[argv.index("--symbol") + 1] == "560780"
    assert "--trade-date" not in argv
    assert options["shell"] is False
    assert options["timeout"] == 12
    assert "task_id" not in json.dumps(result)
    assert "Codex" not in json.dumps(result)


def test_market_context_explicit_trade_date_is_passed_through(tmp_path):
    seen = []

    def runner(argv, **kwargs):
        seen.append(argv)
        payload = _payload()
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    _service(tmp_path, runner=runner).market_context("560780", TARGET)

    assert seen[0][seen[0].index("--trade-date") + 1] == TARGET
    assert seen[0][seen[0].index("--exchange") + 1] == "SH"


def test_weekend_defaults_to_latest_completed_official_trade_date():
    calendar = _calendar_lookup({
        (2026, 9): {"2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"},
    })
    now = datetime(2026, 9, 27, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    target, previous, latest = _resolve_trade_dates(None, now, calendar)

    assert (target, previous, latest) == ("2026-09-25", "2026-09-24", "2026-09-25")


def test_request_local_calendar_reuses_same_month_only_within_request():
    calls = []
    raw = _calendar_rows(2026, 9, {"2026-09-24", "2026-09-25", "2026-09-28"})

    def load(year, month):
        calls.append((year, month))
        return raw

    cached = _request_local_calendar(load)
    now = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
    assert _resolve_trade_dates(None, now, cached) == ("2026-09-28", "2026-09-25", "2026-09-28")
    assert calls == [(2026, 9)]

    calls.clear()
    cached = _request_local_calendar(lambda year, month: calls.append((year, month)) or _calendar_rows(
        year,
        month,
        {
            (2026, 10): {"2026-10-08", "2026-10-09", "2026-10-12"},
            (2026, 9): {"2026-09-28", "2026-09-29", "2026-09-30"},
        }[(year, month)],
    ))
    now = datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc)
    assert _resolve_trade_dates(None, now, cached) == ("2026-09-30", "2026-09-29", "2026-09-30")
    assert calls == [(2026, 10), (2026, 9)]


def test_historical_context_skips_current_quote_but_latest_context_fetches_it(tmp_path, monkeypatch):
    root = tmp_path / "a-stock-data"
    root.mkdir()
    (root / "SKILL.md").write_text("fake adapter skill", encoding="utf-8")
    quote_calls = []
    active_target = ["2026-09-25"]
    open_days = {"2026-09-24", "2026-09-25", "2026-09-28"}
    monkeypatch.setattr(context_adapter, "_load_functions", lambda _root: _adapter_namespace(active_target[0], quote_calls))
    monkeypatch.setattr(context_adapter, "_marker_code", lambda *_args: "def etf_shares(day, exchange='SH'):\n    return []")
    monkeypatch.setattr(
        context_adapter,
        "_load_trading_calendar",
        lambda _root: lambda year, month: _calendar_rows(year, month, open_days),
    )
    now = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)

    historical = run_context_adapter(
        "560780", TARGET, root, exchange="SH", now=now
    )
    assert quote_calls == []
    assert historical["trade_date"] == TARGET
    assert historical["core"]["snapshot"]["status"] == "NOT_APPLICABLE"
    assert historical["core"]["snapshot"]["quote"] is None
    assert historical["core"]["intraday_5m"]["source_trade_date"] == TARGET
    assert historical["core"]["daily"]["source_trade_date"] == TARGET

    active_target[0] = "2026-09-28"
    latest = run_context_adapter("560780", None, root, exchange="SH", now=now)
    assert quote_calls == ["560780"]
    assert latest["trade_date"] == "2026-09-28"
    assert latest["core"]["snapshot"]["status"] == "OK"


def test_historical_snapshot_normalization_requires_no_current_quote(tmp_path):
    payload = _payload()
    payload["core"]["snapshot"].update(
        status="NOT_APPLICABLE",
        source_trade_date=None,
        as_of=None,
        expected_latest_trade_date="2026-09-28",
        quote=None,
    )
    result = _service(tmp_path, payload).market_context("560780", TARGET)
    assert result["core"]["snapshot"]["status"] == "NOT_APPLICABLE"
    assert result["core"]["snapshot"]["data"] is None
    assert result["overall_status"] == "PARTIAL"

    payload["core"]["snapshot"]["status"] = "OK"
    with pytest.raises(MarketDataSourceError):
        _service(tmp_path, payload).market_context("560780", TARGET)


def test_statutory_holiday_defaults_to_last_open_day_of_previous_month():
    calendar = _calendar_lookup({
        (2026, 10): {"2026-10-08", "2026-10-09", "2026-10-12"},
        (2026, 9): {"2026-09-28", "2026-09-29", "2026-09-30"},
        (2026, 8): {"2026-08-31"},
    })
    now = datetime(2026, 10, 3, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    target, previous, latest = _resolve_trade_dates(None, now, calendar)

    assert (target, previous, latest) == ("2026-09-30", "2026-09-29", "2026-09-30")
    with pytest.raises(InvalidTradeDate):
        _resolve_trade_dates("2026-10-01", now, calendar)


def test_explicit_statutory_holiday_is_rejected_even_after_later_sessions():
    calendar = _calendar_lookup({
        (2026, 10): {"2026-10-08", "2026-10-09", "2026-10-12"},
        (2026, 9): {"2026-09-28", "2026-09-29", "2026-09-30"},
    })
    now = datetime(2026, 10, 10, 11, 0, tzinfo=timezone(timedelta(hours=8)))

    with pytest.raises(InvalidTradeDate):
        _resolve_trade_dates("2026-10-01", now, calendar)


def test_intraday_adapter_filters_mixed_trade_dates():
    class Series:
        def dropna(self):
            return self

        def unique(self):
            return ["tencent"]

    class Frame:
        columns = {"datetime", "open", "high", "low", "close", "volume", "source"}

        def __getitem__(self, _key):
            return Series()

        def to_dict(self, orient):
            assert orient == "records"
            return [
                {"datetime": "2026-09-24 14:55", "open": 1, "high": 2, "low": 1, "close": 2, "volume": 10, "source": "tencent"},
                {"datetime": "2026-09-25 09:35", "open": 2, "high": 3, "low": 2, "close": 3, "volume": 20, "source": "tencent"},
                {"datetime": "2026-09-25 09:40", "open": 3, "high": 4, "low": 3, "close": 4, "volume": 30, "source": "tencent"},
            ]

    namespace = {"tencent_kline": lambda *args, **kwargs: Frame()}
    result, _latency = _intraday_block(namespace, "sh560780", TARGET)

    assert [bar["datetime"] for bar in result["bars"]] == ["2026-09-25 09:35", "2026-09-25 09:40"]
    assert result["source_trade_date"] == TARGET


def test_shares_one_day_failure_is_partial(tmp_path):
    payload = _payload()
    payload["etf_shares"]["previous"].update(status="UNAVAILABLE", source_trade_date=None, shares_10k=None, as_of=None)

    result = _service(tmp_path, payload).market_context("560780")

    assert result["overall_status"] == "PARTIAL"
    assert result["core"]["snapshot"]["status"] == "OK"
    assert result["enrichment"]["etf_shares"]["status"] == "PARTIAL"
    assert result["enrichment"]["etf_shares"]["shares_latest"] == 123456.0
    assert result["enrichment"]["etf_shares"]["shares_previous"] is None
    assert result["enrichment"]["etf_shares"]["change_absolute"] is None


def test_shares_timeout_does_not_drop_core(tmp_path):
    payload = _payload()
    payload["etf_shares"]["latest"].update(status="TIMEOUT", source_trade_date=None, shares_10k=None, as_of=None)
    payload["etf_shares"]["previous"].update(status="TIMEOUT", source_trade_date=None, shares_10k=None, as_of=None)

    result = _service(tmp_path, payload).market_context("560780")

    assert result["overall_status"] == "PARTIAL"
    assert result["core"]["daily"]["status"] == "OK"
    assert result["enrichment"]["etf_shares"]["status"] == "TIMEOUT"


def test_verified_registry_controls_etf_identity_and_tracking_index_is_separate():
    service = MarketContextService(None)
    registry_entry = service._registry["instruments"]["560780"]
    assert registry_entry == {
        "symbol": "560780",
        "asset_type": "ETF",
        "exchange": "SH",
        "name": "半导体材料ETF",
        "verification_status": "VERIFIED",
        "tracking_index": {
            "code": "931743",
            "name": "中证半导体材料设备主题指数",
            "verification_status": "VERIFIED",
        },
    }
    for symbol in ("600000", "000001", "501018"):
        with pytest.raises(MarketDataRequestError):
            service._validate_request(symbol, None)
    service._validate_request("560780", None)

    tracking = service._tracking_index("560780")
    assert tracking["code"] == "931743"
    assert tracking["identity_status"] == "VERIFIED"
    assert tracking["market_data_status"] == "UNAVAILABLE"

    assert service._registry["market_references"] == []
    assert all(ref.get("role") == "MARKET_REFERENCE" for ref in service._registry["market_references"])
    assert "TRACKING_INDEX" not in json.dumps(service._registry["market_references"])


def test_inconsistent_daily_date_fails_minimum_core(tmp_path):
    payload = _payload()
    daily = payload["core"]["daily"]
    daily["status"] = "PARTIAL"
    daily["source_trade_date"] = PREVIOUS
    daily["bars"] = [_bar(day) for day in _weekdays(PREVIOUS)]
    payload["core"]["snapshot"]["freshness"]["daily_kline_stale"] = True

    with pytest.raises(MarketDataSourceError):
        _service(tmp_path, payload).market_context("560780")


@pytest.mark.parametrize("symbol", ["600000", "000001", "501018", "../../secret", "56078x", "", "560780 && whoami"])
def test_invalid_symbols_do_not_start_subprocess(tmp_path, symbol):
    calls = []
    service = _service(tmp_path, runner=lambda *args, **kwargs: calls.append(args))

    with pytest.raises(MarketDataRequestError):
        service.market_context(symbol)

    assert calls == []


@pytest.mark.parametrize("symbol", ["600000", "000001", "501018"])
def test_mcp_rejects_symbols_missing_from_verified_registry(tmp_path, symbol):
    calls = []
    service = _service(tmp_path, runner=lambda *args, **kwargs: calls.append(args))

    result = _mcp_result(service, {"symbol": symbol})

    assert result["isError"] is True
    assert result["structuredContent"]["error_code"] == "INVALID_REQUEST"
    assert calls == []


@pytest.mark.parametrize("trade_date", ["2026-9-25", "2026-02-30", "yesterday", "2026/09/25"])
def test_invalid_trade_date_format_does_not_start_subprocess(tmp_path, trade_date):
    calls = []
    service = _service(tmp_path, runner=lambda *args, **kwargs: calls.append(args))

    with pytest.raises(MarketDataRequestError):
        service.market_context("560780", trade_date)

    assert calls == []


def test_adapter_resolved_holiday_becomes_invalid_request_without_raw_error(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, '{"request_error":"INVALID_TRADE_DATE"}', "")

    result = _mcp_result(_service(tmp_path, runner=runner), {"symbol": "560780", "trade_date": "2026-10-01"})

    assert result["isError"] is True
    assert result["structuredContent"]["error_code"] == "INVALID_REQUEST"
    assert "2026-10-01" not in json.dumps(result)


def test_malicious_extra_subprocess_fields_are_not_forwarded(tmp_path):
    payload = _payload()
    payload["private_path"] = _SECRET_PATH
    payload["core"]["snapshot"]["quote"]["credential"] = _SECRET_TOKEN
    payload["core"]["daily"]["bars"][0]["cookie"] = "session-cookie-secret"
    payload["core"]["snapshot"]["warnings"] = []

    result = _service(tmp_path, payload).market_context("560780")
    public = json.dumps(result, ensure_ascii=False)

    for unsafe in (_SECRET_PATH, "sk-test-secret", "session-cookie-secret", "credential", "cookie"):
        assert unsafe not in public


def test_nonzero_exit_redacts_path_token_traceback_and_argv(tmp_path):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 9, "", f"{_SECRET_PATH}\n{_SECRET_TOKEN}\nTraceback (most recent call last): {argv}")

    result = _mcp_result(_service(tmp_path, runner=runner), {"symbol": "560780"})
    public = json.dumps(result, ensure_ascii=False)

    assert result["isError"] is True
    assert result["structuredContent"]["error_code"] == "MARKET_DATA_SOURCE_FAILED"
    for unsafe in (_SECRET_PATH, "sk-test-secret", "Traceback", "argv", "python.exe"):
        assert unsafe not in public


def test_invalid_json_and_outer_timeout_are_safe_public_errors(tmp_path):
    bad_json = _service(
        tmp_path,
        runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, f"not json {_SECRET_PATH}", ""),
    )
    invalid = _mcp_result(bad_json, {"symbol": "560780"})
    assert invalid["structuredContent"]["error_code"] == "MARKET_DATA_SOURCE_FAILED"
    assert _SECRET_PATH not in json.dumps(invalid)

    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=_SECRET_PATH, stderr=_SECRET_TOKEN)

    timed = _mcp_result(_service(tmp_path, runner=timeout), {"symbol": "560780"})
    public = json.dumps(timed)
    assert timed["structuredContent"]["error_code"] == "MARKET_DATA_TIMEOUT"
    assert _SECRET_PATH not in public and "sk-test-secret" not in public


def test_payload_hard_limit_rejects_oversized_child_output(tmp_path):
    oversized = json.dumps({"pad": "x" * (40 * 1024)}, separators=(",", ":"))
    service = _service(tmp_path, runner=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, oversized, ""))

    with pytest.raises(MarketDataSourceError):
        service.market_context("560780")


def test_no_usable_core_blocks_returns_a_true_public_error(tmp_path):
    payload = _payload()
    for name in ("snapshot", "intraday_5m", "daily"):
        payload["core"][name]["status"] = "UNAVAILABLE"
        payload["core"][name]["source_trade_date"] = None
        if name == "snapshot":
            payload["core"][name]["quote"] = None
        else:
            payload["core"][name]["bars"] = []
    result = _mcp_result(_service(tmp_path, payload), {"symbol": "560780"})

    assert result["isError"] is True
    assert result["structuredContent"]["error_code"] == "MARKET_DATA_SOURCE_FAILED"


@pytest.mark.parametrize(
    ("case", "snapshot", "intraday", "daily", "shares", "expected_status", "mcp_error"),
    [
        ("A", "OK", "OK", "OK", "OK", "OK", False),
        ("B", "OK", "OK", "OK", "TIMEOUT", "PARTIAL", False),
        ("C", "ERROR", "OK", "OK", "OK", "PARTIAL", False),
        ("D", "OK", "ERROR", "OK", "OK", None, True),
        ("E", "OK", "OK", "ERROR", "OK", None, True),
        ("F", "ERROR", "ERROR", "OK", "OK", None, True),
        ("G", "OK", "ERROR", "ERROR", "OK", None, True),
        ("H", "ERROR", "ERROR", "ERROR", "ERROR", None, True),
    ],
)
def test_minimum_viable_core_requires_target_day_5m_and_daily(
    tmp_path, case, snapshot, intraday, daily, shares, expected_status, mcp_error
):
    payload = _payload()
    if snapshot != "OK":
        payload["core"]["snapshot"].update(status=snapshot, source_trade_date=None, as_of=None, quote=None)
    if intraday != "OK":
        payload["core"]["intraday_5m"].update(status=intraday, source_trade_date=None, bars=[])
    if daily != "OK":
        payload["core"]["daily"].update(status=daily, source_trade_date=None, bars=[])
    if shares == "TIMEOUT":
        for observation in ("latest", "previous"):
            payload["etf_shares"][observation].update(
                status="TIMEOUT", source_trade_date=None, shares_10k=None, as_of=None
            )
    elif shares == "ERROR":
        for observation in ("latest", "previous"):
            payload["etf_shares"][observation].update(
                status="ERROR", source_trade_date=None, shares_10k=None, as_of=None
            )

    result = _mcp_result(_service(tmp_path, payload), {"symbol": "560780"})

    assert result["isError"] is mcp_error
    if mcp_error:
        public = json.dumps(result, ensure_ascii=False)
        assert result["structuredContent"]["error_code"] == "MARKET_DATA_SOURCE_FAILED"
        for unsafe in (_SECRET_PATH, "Traceback", "python.exe", "stderr"):
            assert unsafe not in public
    else:
        assert result["structuredContent"]["overall_status"] == expected_status


def test_mcp_tools_list_keeps_market_context_contract_description():
    response = McpStdioServer(BridgeMcpTools(supervisor=_ForbiddenSupervisor())).handle_message(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    )
    tools = response["result"]["tools"]
    assert len(tools) == 11
    definition = next(tool for tool in tools if tool["name"] == "bridge_market_context")
    description = definition["description"]
    for phrase in (
        "verified local instrument registry",
        "completed-trading-day",
        "Historical trade dates never include a current quote",
        "Deterministic",
        "read-only",
        "Codex",
        "ETF-share enrichment may fail",
    ):
        assert phrase in description


def test_mcp_tools_list_grows_to_eleven_without_changing_previous_tools():
    response = McpStdioServer(BridgeMcpTools(supervisor=_ForbiddenSupervisor())).handle_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    )
    names = {item["name"] for item in response["result"]["tools"]}

    assert len(names) == 11
    assert names == set(BridgeMcpTools._SCHEMAS)
    assert "bridge_market_context" in names
    assert "bridge_market_snapshot" in names
    assert "bridge_start_code_task" in names
