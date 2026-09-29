"""Load the published a-stock-data functions and call them read-only."""

from __future__ import annotations

import argparse
import ast
import calendar as _calendar
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import math
import os
import re
import ssl
import sys
import time
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from typing import Any


_SYMBOL = re.compile(r"^[0-9]{6}$")
_ALLOWED_ADJUST = {"qfq", "hfq", "none"}
_CN_TZ = timezone(timedelta(hours=8))
_MARKET_CLOSE = datetime_time(15, 0)


def _python_blocks(skill: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", skill, re.S)


def _block_defining(skill: str, name: str) -> str:
    hits = [block for block in _python_blocks(skill) if re.search(rf"^def {re.escape(name)}\(", block, re.M)]
    if len(hits) != 1:
        raise RuntimeError("published function definition unavailable")
    return hits[0]


def _marker_code(skill: str, marker: str) -> str:
    start = f"<!-- {marker}:start -->"
    end = f"<!-- {marker}:end -->"
    if skill.count(start) != 1 or skill.count(end) != 1:
        raise RuntimeError("published source marker unavailable")
    section = skill.split(start, 1)[1].split(end, 1)[0]
    match = re.search(r"```python\n(.*?)\n```", section, re.S)
    if not match:
        raise RuntimeError("published source block unavailable")
    return match.group(1)


def _definitions_only(source: str) -> Any:
    """Strip walkthrough calls from a documented code block before execution."""
    tree = ast.parse(source)
    function_names = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    kept = []
    for node in tree.body:
        if isinstance(node, (ast.Expr, ast.For, ast.While, ast.With, ast.AsyncWith)):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            calls = {
                call.func.id
                for call in ast.walk(node)
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            }
            if calls & function_names:
                continue
        kept.append(node)
    tree.body = kept
    return compile(tree, "a-stock-data:published-function", "exec")


def _load_functions(repo_root: Path) -> dict[str, Any]:
    skill = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    blocks = [
        _definitions_only(_block_defining(skill, name))
        for name in ("get_prefix", "norm_ticker", "tencent_quote")
    ]
    blocks.extend(
        compile(_marker_code(skill, marker), f"a-stock-data:{marker}", "exec")
        for marker in ("v39-helpers", "v39-tencent-kline")
    )
    for block in blocks:
        exec(block, namespace)
    return namespace


def _load_trading_calendar(repo_root: Path):
    """Load a-stock-data's published SZSE calendar with a bounded request timeout."""
    skill = (repo_root / "SKILL.md").read_text(encoding="utf-8")
    namespace: dict[str, Any] = {}
    source = _marker_code(skill, "official-data-core")
    exec(compile(source, "a-stock-data:official-data-core", "exec"), namespace)
    requests = namespace["requests"]

    def bounded_official_get(url, params=None, referer=None):
        response = requests.get(
            url,
            params=params,
            headers={"User-Agent": "Mozilla/5.0", "Referer": referer or url},
            timeout=(3, 5),
            verify=True,
        )
        response.raise_for_status()
        return response

    # Keep TLS verification enabled and cap calendar latency so an optional
    # calendar outage can degrade to UNKNOWN within the Bridge task timeout.
    namespace["_official_get"] = bounded_official_get
    return namespace["trading_calendar"]


def _complete_calendar(calendar_value: Any, year: int, month: int) -> dict[str, bool]:
    """Return a validated date->open map; reject partial/ambiguous calendars."""
    if hasattr(calendar_value, "to_dict"):
        rows = calendar_value.to_dict(orient="records")
    else:
        rows = calendar_value
    if not isinstance(rows, list) or not rows:
        raise ValueError("calendar unavailable")

    last_day = _calendar.monthrange(year, month)[1]
    expected = {
        date(year, month, day).isoformat()
        for day in range(1, last_day + 1)
    }
    result: dict[str, bool] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("calendar row invalid")
        raw_day = row.get("date")
        is_open = row.get("is_open")
        if not isinstance(raw_day, str) or type(is_open) is not bool:
            raise ValueError("calendar row invalid")
        try:
            normalized_day = date.fromisoformat(raw_day).isoformat()
        except ValueError as exc:
            raise ValueError("calendar date invalid") from exc
        if normalized_day != raw_day or normalized_day in result:
            raise ValueError("calendar date invalid")
        result[normalized_day] = is_open
    if set(result) != expected:
        raise ValueError("calendar incomplete")
    return result


def _previous_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def _resolve_market_context(now: datetime, calendar_lookup) -> dict[str, Any]:
    """Resolve exchange date/status only from a complete official calendar."""
    local_now = now.astimezone(_CN_TZ) if now.tzinfo is not None else None
    market_date = local_now.date().isoformat() if local_now is not None else None
    context = {
        "market_date": market_date,
        "expected_latest_trade_date": None,
        "market_status": "UNKNOWN",
    }
    if local_now is None:
        return context

    try:
        current = _complete_calendar(calendar_lookup(local_now.year, local_now.month), local_now.year, local_now.month)
    except Exception:
        return context

    is_open_day = current.get(market_date)
    if is_open_day is None:
        return context

    local_time = local_now.timetz().replace(tzinfo=None)
    in_session = (
        datetime_time(9, 30) <= local_time < datetime_time(11, 30)
        or datetime_time(13, 0) <= local_time < _MARKET_CLOSE
    )
    context["market_status"] = "OPEN" if is_open_day and in_session else "CLOSED"

    # A daily bar is expected only after the current trading day has closed.
    # Before 15:00, compare against the latest previously completed trade date.
    include_market_date = not is_open_day or local_time >= _MARKET_CLOSE
    eligible = [
        day for day, is_open in current.items()
        if is_open and (day <= market_date if include_market_date else day < market_date)
    ]
    if eligible:
        context["expected_latest_trade_date"] = max(eligible)
        return context

    previous_year, previous_month = _previous_month(local_now.year, local_now.month)
    try:
        previous = _complete_calendar(
            calendar_lookup(previous_year, previous_month), previous_year, previous_month
        )
    except Exception:
        return context
    previous_eligible = [day for day, is_open in previous.items() if is_open]
    if previous_eligible:
        context["expected_latest_trade_date"] = max(previous_eligible)
    return context


def _exact_quote_timestamp(value: Any) -> str | None:
    """Preserve only an actual timezone-qualified source timestamp."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.isoformat()


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--days", required=True, type=int)
    parser.add_argument("--adjust", required=True, choices=sorted(_ALLOWED_ADJUST))
    return parser


def _latest_none_reference(namespace: dict[str, Any], code: str) -> dict[str, Any] | None:
    """Fetch a small, independently validated unadjusted daily reference."""
    # Keep this optional request bounded across all Tencent fallback hosts. It
    # runs only after the requested daily series has completed.
    original_http = namespace["_v39_http"]

    def bounded_http(url, *args, **kwargs):
        kwargs["timeout"] = (2, 3)
        return original_http(url, *args, **kwargs)

    namespace["_v39_http"] = bounded_http
    try:
        frame = namespace["tencent_kline"](code, period="day", adjust="", count=2)
    except Exception:
        return None
    finally:
        namespace["_v39_http"] = original_http

    try:
        required = {"date", "open", "high", "low", "close", "volume", "source"}
        if frame is None or not hasattr(frame, "columns") or not required.issubset(frame.columns) or frame.empty:
            return None
        if {str(source) for source in frame["source"].dropna().unique()} != {"tencent"}:
            return None
        raw = frame.tail(1).to_dict(orient="records")[0]
        day = raw["date"]
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            return None
        bar: dict[str, Any] = {"date": day}
        for field in ("open", "high", "low", "close", "volume"):
            value = raw[field]
            item = getattr(value, "item", None)
            value = item() if callable(item) else value
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return None
            bar[field] = value
        if min(bar[field] for field in ("open", "high", "low", "close")) <= 0:
            return None
        if bar["high"] < bar["low"] or bar["volume"] < 0:
            return None
        return {"source": "tencent", "adjust": "none", "bar": bar}
    except Exception:
        # Reference validation is enrichment; no malformed source object may
        # turn a successful requested snapshot into a public tool error.
        return None


def run(args: argparse.Namespace) -> dict[str, Any]:
    if not _SYMBOL.fullmatch(args.symbol) or not 1 <= args.days <= 250:
        raise ValueError("invalid request")
    if args.adjust not in _ALLOWED_ADJUST:
        raise ValueError("invalid request")

    import certifi

    ca_bundle = certifi.where()
    os.environ["SSL_CERT_FILE"] = ca_bundle
    os.environ["REQUESTS_CA_BUNDLE"] = ca_bundle
    ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=os.environ["SSL_CERT_FILE"])

    started = time.perf_counter()
    namespace = _load_functions(args.repo_root)
    reference_namespace = None
    if args.adjust == "qfq":
        try:
            # The published helper keeps mutable host fallback state in its
            # globals. A separate namespace lets both K-line calls overlap.
            reference_namespace = _load_functions(args.repo_root)
        except Exception:
            pass  # Optional quality enrichment must not fail the snapshot.
    code = namespace["norm_ticker"](args.symbol, stock_only=True)
    setup_ms = round((time.perf_counter() - started) * 1000, 3)

    adjust = "" if args.adjust == "none" else args.adjust

    def fetch_quote() -> tuple[dict[str, Any], float]:
        quote_started = time.perf_counter()
        quotes = namespace["tencent_quote"]([code])
        elapsed_ms = round((time.perf_counter() - quote_started) * 1000, 3)
        value = quotes.get(code)
        if not isinstance(value, dict) or value.get("is_stale") is True:
            raise RuntimeError("quote unavailable")
        return value, elapsed_ms

    def fetch_kline() -> tuple[Any, float]:
        kline_started = time.perf_counter()
        value = namespace["tencent_kline"](code, period="day", adjust=adjust, count=args.days)
        elapsed_ms = round((time.perf_counter() - kline_started) * 1000, 3)
        return value, elapsed_ms

    def fetch_reference() -> tuple[dict[str, Any] | None, float]:
        reference_started = time.perf_counter()
        value = _latest_none_reference(reference_namespace, code)
        elapsed_ms = round((time.perf_counter() - reference_started) * 1000, 3)
        return value, elapsed_ms

    # The optional reference overlaps source waits without a second process.
    with ThreadPoolExecutor(max_workers=3 if reference_namespace is not None else 2) as pool:
        quote_future = pool.submit(fetch_quote)
        kline_future = pool.submit(fetch_kline)
        reference_future = pool.submit(fetch_reference) if reference_namespace is not None else None
        quote, quote_ms = quote_future.result()
        frame, kline_ms = kline_future.result()
        try:
            none_reference, reference_ms = reference_future.result() if reference_future is not None else (None, 0.0)
        except Exception:
            none_reference, reference_ms = None, 0.0

    required_columns = {"date", "open", "high", "low", "close", "volume", "source"}
    if frame is None or frame.empty or not required_columns.issubset(frame.columns):
        raise RuntimeError("daily K-line unavailable")
    actual_sources = {str(source) for source in frame["source"].dropna().unique()}
    if actual_sources != {"tencent"}:
        raise RuntimeError("unexpected K-line source")

    rows = []
    for raw in frame.tail(args.days).to_dict(orient="records"):
        row: dict[str, Any] = {"date": str(raw["date"])}
        for field in ("open", "high", "low", "close", "volume"):
            value = raw[field]
            item = getattr(value, "item", None)
            row[field] = item() if callable(item) else value
        rows.append(row)

    # The Tencent quote function does not currently expose a quote timestamp.
    # Capture the data retrieval time once, then derive exchange context from
    # the published official calendar. Calendar failures are intentionally
    # non-fatal and yield UNKNOWN without a warning.
    retrieved_at = datetime.now(timezone.utc)
    try:
        calendar_lookup = _load_trading_calendar(args.repo_root)
        market_context = _resolve_market_context(retrieved_at, calendar_lookup)
    except Exception:
        local_now = retrieved_at.astimezone(_CN_TZ)
        market_context = {
            "market_date": local_now.date().isoformat(),
            "expected_latest_trade_date": None,
            "market_status": "UNKNOWN",
        }

    return {
        "symbol": code,
        "name": quote["name"],
        "quote": quote,
        "daily_kline": rows,
        "_none_reference": none_reference,
        "adjust": args.adjust,
        "data_source": {"quote": "tencent", "daily_kline": "tencent"},
        "retrieved_at": retrieved_at.isoformat(),
        "quote_timestamp": _exact_quote_timestamp(quote.get("quote_timestamp")),
        **market_context,
        "_timings_ms": {"adapter_setup": setup_ms, "quote": quote_ms, "daily_kline": kline_ms,
                        "reference": reference_ms},
        "warnings": [],
    }


def main() -> int:
    args = _argument_parser().parse_args()
    # The a-stock-data examples have module-level demonstrations; suppress all source output.
    with contextlib.redirect_stdout(io.StringIO()):
        payload = run(args)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
