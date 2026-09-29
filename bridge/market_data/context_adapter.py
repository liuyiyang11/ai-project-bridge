"""Read-only a-stock-data adapter for the ETF market-context fast path."""

from __future__ import annotations

import argparse
import contextlib
from concurrent.futures import ThreadPoolExecutor
import io
import json
import math
import os
import ssl
import sys
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__:
    from .adapter import _complete_calendar, _exact_quote_timestamp, _load_functions, _load_trading_calendar, _marker_code
else:  # Executed as a deterministic script by MarketContextService.
    from adapter import _complete_calendar, _exact_quote_timestamp, _load_functions, _load_trading_calendar, _marker_code


_CN_TZ = timezone(timedelta(hours=8))
_MARKET_CLOSE = datetime_time(15, 0)
_M5_COUNT = 320
_DAILY_WINDOW_DAYS = 120
_DAILY_LIMIT = 60
_SHARES_CONNECT_TIMEOUT = 2.0
_SHARES_READ_TIMEOUT = 3.0
_TENCENT_CONNECT_TIMEOUT = 1.0
_TENCENT_READ_TIMEOUT = 2.0


class InvalidTradeDate(ValueError):
    """A well-formed date which is not a completed exchange trading day."""


def _validate_registered_etf(symbol: str, exchange: str) -> None:
    """Fail closed against the packaged, manually verified ETF registry."""
    try:
        path = Path(__file__).with_name("instrument_registry.json")
        registry = json.loads(path.read_text(encoding="utf-8"))
        entry = registry["instruments"][symbol]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("unverified ETF instrument") from None
    if (
        not isinstance(entry, dict)
        or entry.get("symbol") != symbol
        or entry.get("asset_type") != "ETF"
        or entry.get("exchange") != exchange
        or entry.get("verification_status") != "VERIFIED"
    ):
        raise ValueError("unverified ETF instrument")


def _month_map(calendar_lookup, year: int, month: int) -> dict[str, bool]:
    raw = calendar_lookup(year, month)
    return _complete_calendar(raw, year, month)


def _request_local_calendar(calendar_lookup):
    """Reuse official raw calendar responses within one context request only."""
    cache = {}

    def lookup(year: int, month: int):
        key = (year, month)
        if key not in cache:
            cache[key] = calendar_lookup(year, month)
        return cache[key]

    return lookup


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _previous_trade_date(target: date, calendar_lookup) -> str:
    current = _month_map(calendar_lookup, target.year, target.month)
    previous = [day for day, opened in current.items() if opened and day < target.isoformat()]
    if previous:
        return max(previous)
    year, month = _previous_month(target.year, target.month)
    prior_month = _month_map(calendar_lookup, year, month)
    earlier = [day for day, opened in prior_month.items() if opened]
    if not earlier:
        raise ValueError("previous trading date unavailable")
    return max(earlier)


def _latest_completed_trade_date(now: datetime, calendar_lookup) -> date:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone required")
    local_now = now.astimezone(_CN_TZ)
    current = _month_map(calendar_lookup, local_now.year, local_now.month)
    today = local_now.date()
    include_today = local_now.timetz().replace(tzinfo=None) >= _MARKET_CLOSE
    eligible = [
        date.fromisoformat(day)
        for day, opened in current.items()
        if opened and (day < today.isoformat() or (day == today.isoformat() and include_today))
    ]
    if eligible:
        return max(eligible)
    year, month = _previous_month(local_now.year, local_now.month)
    previous = _month_map(calendar_lookup, year, month)
    prior = [date.fromisoformat(day) for day, opened in previous.items() if opened]
    if not prior:
        raise ValueError("latest completed trading date unavailable")
    return max(prior)


def _resolve_trade_dates(trade_date: str | None, now: datetime, calendar_lookup) -> tuple[str, str, str]:
    latest = _latest_completed_trade_date(now, calendar_lookup)
    if trade_date is None:
        target = latest
    else:
        if not isinstance(trade_date, str):
            raise InvalidTradeDate("invalid trade date")
        try:
            target = date.fromisoformat(trade_date)
        except ValueError:
            raise InvalidTradeDate("invalid trade date") from None
        if target.isoformat() != trade_date or target > latest:
            raise InvalidTradeDate("trade date is not completed")
        month = _month_map(calendar_lookup, target.year, target.month)
        if month.get(trade_date) is not True:
            raise InvalidTradeDate("trade date is not an exchange session")
    previous = _previous_trade_date(target, calendar_lookup)
    return target.isoformat(), previous, latest.isoformat()


def _plain(value: Any) -> Any:
    item = getattr(value, "item", None)
    return item() if callable(item) else value


def _source_timeout(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".casefold()
    return "timeout" in text or "timed out" in text


def _failure_status(exc: BaseException) -> str:
    return "TIMEOUT" if _source_timeout(exc) else "UNAVAILABLE"


def _fetch_shares(namespace: dict[str, Any], symbol: str, exchange: str, day: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        frame = namespace["etf_shares"](day, exchange)
        rows = frame.to_dict(orient="records") if hasattr(frame, "to_dict") else frame
        if not isinstance(rows, list):
            raise RuntimeError("invalid ETF shares result")
        hits = [row for row in rows if isinstance(row, dict) and row.get("code") == symbol]
        if len(hits) != 1:
            return {
                "status": "UNAVAILABLE",
                "requested_trade_date": day,
                "source_trade_date": None,
                "shares_10k": None,
                "as_of": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        row = hits[0]
        shares = row.get("shares_10k")
        if isinstance(shares, bool) or not isinstance(shares, (int, float)) or not math.isfinite(float(shares)):
            raise RuntimeError("invalid ETF shares value")
        source_day = row.get("date")
        if source_day != day:
            raise RuntimeError("ETF shares date mismatch")
        return {
            "status": "OK",
            "requested_trade_date": day,
            "source_trade_date": source_day,
            "shares_10k": float(shares),
            "as_of": row.get("fetched_at") if isinstance(row.get("fetched_at"), str) else None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    except Exception as exc:
        return {
            "status": _failure_status(exc),
            "requested_trade_date": day,
            "source_trade_date": None,
            "shares_10k": None,
            "as_of": None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        }


def _quote_block(namespace: dict[str, Any], code: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    quotes = namespace["tencent_quote"]([code])
    value = quotes.get(code) if isinstance(quotes, dict) else None
    if not isinstance(value, dict) or value.get("is_stale") is True:
        raise RuntimeError("quote unavailable")
    fields = ("name", "price", "last_close", "open", "high", "low", "change_amt", "change_pct", "amount_wan")
    quote = {key: _plain(value[key]) for key in fields if key in value}
    if not all(key in quote for key in fields[:-1]):
        raise RuntimeError("quote fields unavailable")
    quote_timestamp = _exact_quote_timestamp(value.get("quote_timestamp"))
    if quote_timestamp is not None:
        quote["quote_timestamp"] = quote_timestamp
    quote["source_is_stale"] = value.get("is_stale") if isinstance(value.get("is_stale"), bool) else None
    return quote, round((time.perf_counter() - started) * 1000, 3)


def _intraday_block(namespace: dict[str, Any], code: str, target: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    frame = namespace["tencent_kline"](code, period="m5", adjust="", count=_M5_COUNT)
    required = {"datetime", "open", "high", "low", "close", "volume", "source"}
    if frame is None or not hasattr(frame, "columns") or not required.issubset(frame.columns):
        raise RuntimeError("intraday columns unavailable")
    if {str(item) for item in frame["source"].dropna().unique()} != {"tencent"}:
        raise RuntimeError("unexpected intraday source")
    raw_rows = frame.to_dict(orient="records")
    observed_dates: list[str] = []
    selected: list[dict[str, Any]] = []
    for raw in raw_rows:
        stamp = raw.get("datetime")
        if not isinstance(stamp, str) or len(stamp) != 16:
            raise RuntimeError("invalid intraday datetime")
        stamp_date = date.fromisoformat(stamp[:10]).isoformat()
        datetime.strptime(stamp, "%Y-%m-%d %H:%M")
        observed_dates.append(stamp_date)
        if stamp_date != target:
            continue
        row: dict[str, Any] = {"datetime": stamp}
        for field in ("open", "high", "low", "close", "volume"):
            row[field] = _plain(raw[field])
        selected.append(row)
    selected.sort(key=lambda row: row["datetime"])
    return {
        "bars": selected,
        "source_trade_date": target if selected else max(observed_dates) if observed_dates else None,
    }, round((time.perf_counter() - started) * 1000, 3)


def _daily_block(namespace: dict[str, Any], code: str, target: str) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    end_day = date.fromisoformat(target)
    begin_day = end_day - timedelta(days=_DAILY_WINDOW_DAYS)
    frame = namespace["tencent_kline"](
        code,
        period="day",
        adjust="",
        start=begin_day.isoformat(),
        end=target,
        count=_DAILY_LIMIT,
    )
    required = {"date", "open", "high", "low", "close", "volume", "source"}
    if frame is None or not hasattr(frame, "columns") or not required.issubset(frame.columns):
        raise RuntimeError("daily columns unavailable")
    if {str(item) for item in frame["source"].dropna().unique()} != {"tencent"}:
        raise RuntimeError("unexpected daily source")
    rows: list[dict[str, Any]] = []
    for raw in frame.to_dict(orient="records"):
        day = raw.get("date")
        if not isinstance(day, str):
            raise RuntimeError("invalid daily date")
        parsed_day = date.fromisoformat(day)
        if parsed_day.isoformat() != day:
            raise RuntimeError("invalid daily date")
        if day > target:
            continue
        row: dict[str, Any] = {"date": day}
        for field in ("open", "high", "low", "close", "volume"):
            row[field] = _plain(raw[field])
        rows.append(row)
    rows.sort(key=lambda row: row["date"])
    rows = rows[-_DAILY_LIMIT:]
    return {
        "bars": rows,
        "source_trade_date": rows[-1]["date"] if rows else None,
    }, round((time.perf_counter() - started) * 1000, 3)


def run(
    symbol: str,
    trade_date: str | None,
    repo_root: Path,
    *,
    exchange: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not isinstance(symbol, str) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("invalid symbol")
    if exchange not in {"SH", "SZ"}:
        raise ValueError("invalid exchange")
    _validate_registered_etf(symbol, exchange)

    import certifi

    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])

    total_started = time.perf_counter()
    setup_started = time.perf_counter()
    namespace = _load_functions(repo_root)
    exec(compile(_marker_code((repo_root / "SKILL.md").read_text(encoding="utf-8"), "v39-etf-shares"),
                 "a-stock-data:v39-etf-shares", "exec"), namespace)
    original_http = namespace["_v39_http"]

    def bounded_http(url, *args, **kwargs):
        if "query.sse.com.cn" in url or "fund.szse.cn" in url:
            kwargs["timeout"] = (_SHARES_CONNECT_TIMEOUT, _SHARES_READ_TIMEOUT)
        elif any(host in url for host in ("web.ifzq.gtimg.cn", "proxy.finance.qq.com", "ifzq.gtimg.cn")):
            # The published helper defaults to 8s connect / 20s read per Tencent
            # host. Context has a much shorter total budget; retain its ordered
            # host fallback while bounding each attempt for this fast path.
            kwargs["timeout"] = (_TENCENT_CONNECT_TIMEOUT, _TENCENT_READ_TIMEOUT)
        return original_http(url, *args, **kwargs)

    namespace["_v39_http"] = bounded_http
    calendar_lookup = _request_local_calendar(_load_trading_calendar(repo_root))
    calendar_started = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    target, previous, expected_latest = _resolve_trade_dates(trade_date, now, calendar_lookup)
    calendar_ms = round((time.perf_counter() - calendar_started) * 1000, 3)
    code = namespace["norm_ticker"](symbol, stock_only=True)
    if code != symbol:
        raise ValueError("symbol normalization mismatch")
    setup_ms = round((time.perf_counter() - setup_started) * 1000, 3)

    blocks: dict[str, Any] = {
        "snapshot": {"status": "ERROR", "source": "tencent", "quote": None, "freshness": {}, "warnings": []},
        "intraday_5m": {"status": "ERROR", "source": "tencent", "bars": [], "source_trade_date": None, "warnings": []},
        "daily": {"status": "ERROR", "source": "tencent", "bars": [], "source_trade_date": None, "warnings": []},
    }
    snapshot_applicable = target == expected_latest
    if not snapshot_applicable:
        blocks["snapshot"]["status"] = "NOT_APPLICABLE"
    timings: dict[str, float] = {"adapter_setup": setup_ms, "calendar": calendar_ms}

    # Historical contexts never fetch a current quote; target-day bars are the
    # price facts for that completed session.
    with ThreadPoolExecutor(max_workers=2) as pool:
        quote_future = pool.submit(_quote_block, namespace, code) if snapshot_applicable else None
        intraday_future = pool.submit(_intraday_block, namespace, code, target)
        if quote_future is not None:
            try:
                quote, timings["quote"] = quote_future.result()
                blocks["snapshot"].update(status="OK", quote=quote)
            except Exception as exc:
                blocks["snapshot"].update(status=_failure_status(exc), warnings=["SOURCE_TIMEOUT" if _source_timeout(exc) else "SOURCE_UNAVAILABLE"])
        try:
            intraday, timings["intraday_5m"] = intraday_future.result()
            bars = intraday["bars"]
            status = "OK" if bars else "UNAVAILABLE"
            warning = [] if bars else ["TARGET_DATE_NOT_RETURNED"]
            blocks["intraday_5m"].update(
                status=status,
                bars=bars,
                source_trade_date=intraday["source_trade_date"],
                warnings=warning,
            )
        except Exception as exc:
            blocks["intraday_5m"].update(
                status=_failure_status(exc),
                warnings=["SOURCE_TIMEOUT" if _source_timeout(exc) else "SOURCE_UNAVAILABLE"],
            )

    # Keep the second Tencent K-line request serial with the first one.
    daily_started = time.perf_counter()
    try:
        daily, _ = _daily_block(namespace, code, target)
        bars = daily["bars"]
        latest = daily["source_trade_date"]
        status = "OK" if latest == target and len(bars) == _DAILY_LIMIT else "PARTIAL" if bars else "UNAVAILABLE"
        warnings = []
        if bars and latest != target:
            warnings.append("SOURCE_BEHIND_REQUESTED_DATE")
        if bars and len(bars) < _DAILY_LIMIT:
            warnings.append("LESS_THAN_60_BARS_AVAILABLE")
        blocks["daily"].update(status=status, bars=bars, source_trade_date=latest, warnings=warnings)
    except Exception as exc:
        blocks["daily"].update(
            status=_failure_status(exc),
            warnings=["SOURCE_TIMEOUT" if _source_timeout(exc) else "SOURCE_UNAVAILABLE"],
        )
    timings["daily"] = round((time.perf_counter() - daily_started) * 1000, 3)

    quote = blocks["snapshot"].get("quote")
    quote_timestamp = quote.get("quote_timestamp") if isinstance(quote, dict) else None
    try:
        quote_date = datetime.fromisoformat(quote_timestamp.replace("Z", "+00:00")).astimezone(_CN_TZ).date().isoformat()
    except (AttributeError, ValueError):
        quote_date = None
    blocks["snapshot"]["source_trade_date"] = quote_date
    blocks["snapshot"]["as_of"] = quote_timestamp
    blocks["snapshot"]["expected_latest_trade_date"] = expected_latest
    daily_date = blocks["daily"].get("source_trade_date")
    blocks["snapshot"]["freshness"] = {
        "quote_stale": quote_date < expected_latest if quote_date is not None else None,
        "daily_kline_stale": daily_date < target if isinstance(daily_date, str) else None,
        "date_consistent": quote_date == daily_date if quote_date is not None and isinstance(daily_date, str) else None,
        "source_is_stale": quote.get("source_is_stale") if isinstance(quote, dict) else None,
    }

    # Shares are optional. The two bounded exchange reads happen after CORE and
    # run with a maximum of two concurrent requests; failures stay local to enrichment.
    shares_started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2) as pool:
        latest_future = pool.submit(_fetch_shares, namespace, symbol, exchange, target)
        previous_future = pool.submit(_fetch_shares, namespace, symbol, exchange, previous)
        latest_shares = latest_future.result()
        previous_shares = previous_future.result()
    timings["shares"] = round((time.perf_counter() - shares_started) * 1000, 3)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    timings["total"] = round((time.perf_counter() - total_started) * 1000, 3)

    return {
        "symbol": symbol,
        "trade_date": target,
        "previous_trade_date": previous,
        "retrieved_at": retrieved_at,
        "core": blocks,
        "etf_shares": {"latest": latest_shares, "previous": previous_shares, "source": "sse" if exchange == "SH" else "szse"},
        "_timings_ms": timings,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--exchange", required=True, choices=("SH", "SZ"))
    parser.add_argument("--trade-date", default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.trade_date is not None:
        try:
            parsed = date.fromisoformat(args.trade_date)
        except ValueError as exc:
            raise SystemExit(2) from exc
        if parsed.isoformat() != args.trade_date:
            raise SystemExit(2)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            payload = run(args.symbol, args.trade_date, args.repo_root, exchange=args.exchange)
    except InvalidTradeDate:
        payload = {"request_error": "INVALID_TRADE_DATE"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
