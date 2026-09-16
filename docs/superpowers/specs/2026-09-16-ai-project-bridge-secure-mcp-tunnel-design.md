# AI Project Bridge Secure MCP Tunnel Integration Design

**Status:** Approved for Part 1 implementation

**Scope:** Repository-local Secure MCP Tunnel integration only. Account-level onboarding and ChatGPT Web verification are documented but not executed in this phase.

## Goal

Allow the official OpenAI `tunnel-client` to launch the existing AI Project Bridge MCP STDIO server as its `main` MCP child, using a local ignored profile and fail-closed PowerShell helpers, without changing the Bridge runtime or exposing secrets.

## Non-goals and locked boundaries

This phase does not:

- modify `TaskSupervisor`, `WorkerQueue`, `TaskRunner`, the task state machine, Codex reconnect/session logic, or worktree isolation;
- add an HTTP MCP server, WebSocket transport, public proxy, custom authentication, or tunnel implementation;
- log in to OpenAI, create/update/delete/reuse a tunnel, or operate the ChatGPT Web UI;
- read, store, print, commit, or generate API keys, admin keys, tunnel tokens, or other credentials;
- make ordinary tests access the internet or require a real tunnel.

The only tunnel implementation used by the repository is the externally installed official `openai/tunnel-client` binary.

## Current architecture

The existing local MCP entrypoint is:

```text
python -m bridge.mcp.server --config <local config>
```

It is newline-delimited JSON-RPC over STDIO and emits protocol responses only on stdout. Its nine public tools call the existing `TaskSupervisor` through `BridgeMcpTools`; the MCP adapter does not itself start Codex, execute shell commands, or expose a network listener.

The new integration preserves that boundary:

```text
ChatGPT Web (future Part 2)
  -> OpenAI Secure MCP Tunnel
  -> official tunnel-client
  -> MCP STDIO child: bridge.mcp.server
  -> existing Bridge MCP tools and TaskSupervisor
```

## Components

### `bridge.tunnel_profile`

Add a pure Python module responsible for generating and validating a tunnel-client YAML profile. It must not perform network access, start processes, read authentication credentials, or import the Bridge runtime supervisors.

The public behavior is:

- generate an official schema version 1 profile from an absolute Python executable and Bridge config path;
- validate profile structure and the Bridge-specific invariants;
- validate tunnel ID format when given a value for local preflight;
- classify safe tunnel-client doctor failures into the required operational layers.

The generated profile uses the official named-profile schema and contains:

```yaml
config_version: 1
control_plane:
  base_url: https://api.openai.com
  api_key: env:CONTROL_PLANE_API_KEY
health:
  listen_addr: 127.0.0.1:8080
  url_file: <absolute ignored .bridge path>/tunnel-health.url
admin_ui:
  open_browser: false
mcp:
  commands:
    - channel: main
      command: '"<python executable>" -m bridge.mcp.server --config "<config.local.yaml>"'
```

`CONTROL_PLANE_TUNNEL_ID` is intentionally supplied by the environment at runtime rather than written into the generated profile. The API key is represented only by the official `env:CONTROL_PLANE_API_KEY` reference. No `mcp.server_urls` entry is generated.

The validator accepts only the generated profile's minimum field whitelist; this intentionally excludes optional tunnel-client sections such as MCP extra headers, proxy settings, and cloudflared overrides. It rejects missing or incompatible schema fields, non-`main` or duplicate command channels, HTTP MCP bindings, missing `bridge.mcp.server`/`--config`, non-absolute local paths, and literal secret-bearing fields. It returns safe error messages without echoing secret values.

### PowerShell helpers

Add three thin Windows helpers:

- `scripts/tunnel_profile.ps1` invokes the Python profile generator and writes the local ignored profile. It accepts explicit `-PythonExecutable`, `-ConfigPath`, and `-ProfilePath` values, with documented defaults/overrides.
- `scripts/tunnel_doctor.ps1` checks the official executable, required environment variable presence and tunnel ID format, profile validity, then runs `tunnel-client doctor --profile-file <profile> --explain`.
- `scripts/tunnel_start.ps1` performs the same fail-closed preflight and starts `tunnel-client run --profile-file <profile>` in the foreground.

The scripts may report whether a variable is `SET` or `UNSET`, but never print its value. They must not place credentials in command-line arguments. The start helper does not create a background supervisor; the official foreground `run` command remains responsible for the long-lived tunnel process.

Doctor failures are reported with one of:

```text
CONTROL_PLANE
TUNNEL_AUTH
LOCAL_MCP_START
MCP_INITIALIZE
TOOLS_DISCOVERY
CODEX_DISCOVERY
```

The classifier is conservative and safe: authentication markers take precedence over generic control-plane text, and the original client output is sanitized before display, including `Authorization: Bearer ...`, `Authorization=...`, and common key-shaped values.

### Documentation

Add `docs/chatgpt-web-tunnel.md` describing:

- the local MCP-client path versus the future ChatGPT Web path;
- official tunnel-client installation sources and current CLI commands;
- runtime API key versus admin API key responsibilities;
- local profile generation, doctor, start, and health/readiness/UI checks;
- ChatGPT Web connector setup using `Connection: Tunnel` and the tunnel ID;
- manual acceptance order for tool discovery, code-task review/accept, steer, and interrupt;
- which steps are intentionally not executed in Part 1.

Update `docs/mcp-client-setup.md` so it no longer says that ChatGPT Desktop directly starts the local Python STDIO server. It must distinguish compatible local MCP clients from ChatGPT Web through Secure MCP Tunnel.

Update the adjacent `docs/mcp-tools.md` cross-reference so it likewise names compatible local MCP clients and points ChatGPT Web readers to `docs/chatgpt-web-tunnel.md`.

### Tests

Add offline unit tests for profile generation, validation, secret-reference invariants, Windows paths containing spaces, tunnel ID validation, fail-closed prerequisites, and failure-layer classification.

Add an opt-in real doctor smoke entry guarded by `RUN_SECURE_MCP_TUNNEL_SMOKE=1`; the default test suite must skip it and must not make any OpenAI network request. ChatGPT Web calls and the three real end-to-end lifecycle smokes remain manual Part 2 acceptance steps.

## Error handling

- Missing `tunnel-client`, missing profile, missing runtime key, missing tunnel ID, invalid tunnel ID, missing local Python executable, or missing Bridge config fail before any remote call.
- Profile/schema problems are reported as validation failures with no secret echo.
- Official doctor/run exit codes are preserved where possible, and failure output is labeled with the most specific required layer.
- No failure path falls back to an arbitrary MCP URL, arbitrary command, arbitrary cwd, or arbitrary project path.

## Verification

Part 1 is complete only when:

1. the new profile module has red-green unit-test evidence and all offline tests pass;
2. generated profiles validate against the official schema shape and contain only environment references for credentials;
3. the PowerShell helpers fail closed when the binary, profile, runtime key, or tunnel ID is absent;
4. `docs/chatgpt-web-tunnel.md` contains the exact Part 2 commands and manual acceptance sequence;
5. `pytest -q` passes without OpenAI network access;
6. the final report explicitly lists all account-level, tunnel-runtime, and ChatGPT Web checks that remain unverified.
