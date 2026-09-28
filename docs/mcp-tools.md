# MCP stdio tools

Start the local adapter with the standalone module entry point:

```powershell
& $Python -m bridge.mcp.server --config config.local.yaml
```

The legacy equivalent remains available:

```powershell
& $Python -m bridge --config config.local.yaml mcp-stdio
```

The adapter is intended for MCP-compatible local clients. See
[mcp-client-setup.md](mcp-client-setup.md) for compatible local MCP client setup, or [chatgpt-web-tunnel.md](chatgpt-web-tunnel.md) for ChatGPT Web through Secure MCP Tunnel.

The server exposes exactly these tools. V0.2.1 makes only code-task startup asynchronous: `bridge_start_code_task` persists a `QUEUED` task and returns immediately while the shared worker runtime advances it in the background.

Its code-task call chain is:

```text
MCP / Transport
      ↓
TaskSupervisor
      ↓
WorkerQueue
      ↓
TaskRunner
      ↓
TaskHandler
      ↓
Executor
      ↓
CodexSessionManager
```

| Tool | Purpose |
| --- | --- |
| `bridge_list_projects` | Registered project IDs and capabilities only; never project roots. |
| `bridge_codex_catalog` | Runtime `model/list` catalog and supported reasoning efforts. |
| `bridge_start_code_task` | The V0.2.1 async code-task entry; returns `task_id`, `state: "QUEUED"`, and `project`. |
| `bridge_start_experiment_review` | Compatibility interface for a registered deterministic command; not migrated to the V0.2.1 async code-task runtime. |
| `bridge_start_presentation_task` | Compatibility interface for presentation input paths; not migrated to the V0.2.1 async code-task runtime. |
| `bridge_task_status` | Reads the current TaskStore snapshot: state, stage, thread/turn IDs, changed files, review flag, and last event sequence. |
| `bridge_task_events` | Bounded incremental safe events written by EventBus. |
| `bridge_control_task` | `steer`, `interrupt`, `continue`, or `accept` with state checks. |
| `bridge_task_artifacts` | Bounded artifact manifest; no arbitrary file reads. |
| `bridge_market_snapshot` | Deterministic, read-only quote and daily K-line fast path with latest-bar price quality. |
| `bridge_market_context` | Deterministic, read-only completed-trading-day review for ETFs in the verified local registry. Historical dates exclude current quotes; target-date 5-minute and daily bars are required, while ETF shares are optional enrichment. |

`bridge_market_context` is synchronous and does not enter the task orchestration chain shown above. It accepts only `VERIFIED` ETF entries in the packaged local registry, validates all source output in a bounded child process, and uses the official exchange calendar for trade dates. A successful response requires target-day 5-minute bars and daily bars to both be `OK`; a missing snapshot or shares enrichment returns `PARTIAL`. If either required bar block is unavailable, the call returns a safe public data-source error. Historical dates mark the snapshot `NOT_APPLICABLE` and do not fetch a current quote. Tracking-index identity and index market-data availability are separate fields; market references use the `MARKET_REFERENCE` role and stay empty in V1.

`bridge_market_snapshot.daily_kline` keeps the requested `qfq`, `hfq`, or `none` source series, including its latest bar. For `qfq`, the Bridge also requests a small `none` daily reference. `latest_price_bar`, when present, is that unadjusted Tencent reference; it is never relabeled as qfq. `data_quality.latest_daily_bar.status` reports `CONSISTENT`, `INCONSISTENT`, or `UNVERIFIED` for the latest price comparison, while `freshness_status` separately reports `VERIFIED`, `MISMATCH`, or `UNKNOWN` against the expected completed trade date. A missing official calendar leaves freshness `UNKNOWN` even if same-date prices can be compared. Only `exact_price_safe: true` permits `daily_kline[-1]` for exact support, resistance, stop, breakout, or current-day OHLC. If it is false or null, use `latest_price_bar` for exact prices only when its freshness is `VERIFIED`; otherwise wait for a verified current-day reference. Older qfq bars remain available for historical trend analysis, with the latest-bar quality status visible. `hfq` is not compared with `none` in this version.

For code startup, the MCP handler calls `TaskSupervisor.start_code_task()` directly rather than selecting a route through generic `start_task()`. WorkerQueue only owns Futures (`submit`, `cancel`, `shutdown`); it does not report business status. `bridge_task_status` therefore reads the durable snapshot rather than a Future.

EventBus is the only runtime entry for business-state changes: it appends events, maintains monotonic `event_seq`, and updates the snapshot state. TaskStore persists files, metadata, and bounded artifact manifests, but MCP handlers and other business code do not call `update_task(state=...)`.

All schemas reject unknown fields. Paths must be project-relative, experiment commands must be registered in `allowed_commands`, and `source_task_id` is resolved through `TaskStore`; a cross-project or untrusted worktree is rejected. WPS, Dashboard, and Web API are outside the V0.2.1 scope.

## Manual real Codex smoke test

The real app-server smoke test is a manual check, not a pytest result. After installing and discovering the Codex executable, run it manually from the repository root:

```powershell
python scripts/codex_smoke_test.py
```

It creates a temporary Codex workspace, runs the bounded hello-task protocol,
prints safe events and newly created files, and removes the temporary workspace
on exit. It does not open the configured project or perform Git operations.
