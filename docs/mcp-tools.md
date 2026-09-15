# MCP stdio tools

Start the local adapter with:

```powershell
& $Python -m bridge --config config.local.yaml mcp-stdio
```

The adapter is intended for MCP-compatible local clients.  A normal ChatGPT Plus chat cannot be assumed to connect to a local MCP stdio process, so GitHub transport remains the current cloud-facing entry point.

The server exposes exactly these tools.  Code-task startup is asynchronous:
`bridge_start_code_task` persists a `QUEUED` task and returns immediately;
the shared worker runtime advances it in the background.

| Tool | Purpose |
| --- | --- |
| `bridge_list_projects` | Registered project IDs and capabilities only; never project roots. |
| `bridge_codex_catalog` | Runtime `model/list` catalog and supported reasoning efforts. |
| `bridge_start_code_task` | Code task in a Bridge-managed isolated worktree. |
| `bridge_start_experiment_review` | Registered deterministic command against an optional trusted candidate worktree. |
| `bridge_start_presentation_task` | Presentation task with project-relative input paths. |
| `bridge_task_status` | State, stage, thread/turn IDs, changed files, review flag, and last event sequence. |
| `bridge_task_events` | Bounded incremental safe event pagination. |
| `bridge_control_task` | `steer`, `interrupt`, `continue`, or `accept` with state checks. |
| `bridge_task_artifacts` | Bounded artifact manifest; no arbitrary file reads. |

All schemas reject unknown fields.  Paths must be project-relative, experiment commands must be registered in `allowed_commands`, and `source_task_id` is resolved through `TaskStore`; a cross-project or untrusted worktree is rejected.

## Manual real Codex smoke test

The real app-server smoke test is never run by pytest.  After installing and
discovering the Codex executable, run it manually from the repository root:

```powershell
python scripts/codex_smoke_test.py
```

It creates a temporary Codex workspace, runs the bounded hello-task protocol,
prints safe events and newly created files, and removes the temporary workspace
on exit.  It does not open the configured project or perform Git operations.
