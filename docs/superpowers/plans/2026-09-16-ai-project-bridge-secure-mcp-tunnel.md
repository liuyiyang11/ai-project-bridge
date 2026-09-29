# AI Project Bridge Secure MCP Tunnel Integration Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Add an offline-testable, secret-safe integration layer that generates and validates an official OpenAI tunnel-client profile and provides fail-closed Windows doctor/start helpers for the existing Bridge MCP STDIO server.

**Architecture:** A pure bridge.tunnel_profile module owns profile construction, invariant validation, environment preflight, diagnostics redaction, and failure-layer classification. PowerShell wrappers resolve local paths and the official executable, invoke the module, and then invoke only official tunnel-client doctor/run commands. Documentation records the later account and ChatGPT Web acceptance path without executing it.

**Tech Stack:** Python 3.9+, PyYAML, pytest, PowerShell, official OpenAI tunnel-client CLI.

---

## File map

- Create: bridge/tunnel_profile.py — pure profile generator/validator, preflight helpers, safe diagnostics classifier, and fixed CLI.
- Create: scripts/tunnel_profile.ps1 — local profile generation wrapper.
- Create: scripts/tunnel_doctor.ps1 — fail-closed preflight and official doctor wrapper.
- Create: scripts/tunnel_start.ps1 — fail-closed preflight and official foreground run wrapper.
- Create: docs/chatgpt-web-tunnel.md — official installation, profile operation, and deferred Part 2 manual acceptance runbook.
- Create: tests/unit/test_tunnel_profile.py — offline tests for all new Python behavior.
- Create: tests/smoke/test_secure_mcp_tunnel.py — default-skipped opt-in doctor smoke entry.
- Modify: docs/mcp-client-setup.md — correct local-client versus ChatGPT Web transport wording.
- Modify: docs/mcp-tools.md — correct the local setup cross-reference and link the Web Tunnel runbook.
- Modify: README.md — add a short link to the Tunnel runbook only.
- Do not modify: bridge/orchestration/**, bridge/codex/**, bridge/worktree.py, bridge/dispatcher.py, or task lifecycle code.

## Task 1: Add failing tests for profile behavior

**Files:**
- Create: tests/unit/test_tunnel_profile.py
- Read-only reference: bridge/config.py, bridge/mcp/server.py

- [ ] Step 1: Write the failing tests.

Use the intended API below and include cases for Windows paths containing spaces, the official schema shape, env-only API-key reference, absent tunnel_id, missing/duplicate/non-main MCP channels, HTTP server_urls, missing --config, non-absolute paths, invalid tunnel IDs, missing environment values, diagnostics redaction, and all six failure layers.

~~~python
from pathlib import Path

import pytest

from bridge.tunnel_profile import (
    TunnelProfileError,
    build_profile,
    classify_doctor_failure,
    sanitize_diagnostics,
    validate_environment,
    validate_profile,
    validate_tunnel_id,
)


def test_build_profile_uses_official_stdio_main_binding_and_env_key(tmp_path):
    python_executable = tmp_path / "Python 3.9" / "python.exe"
    bridge_config = tmp_path / "AI project bridge" / "config.local.yaml"
    health_url = tmp_path / ".bridge" / "tunnel-health.url"

    profile = build_profile(python_executable, bridge_config, health_url)

    assert profile["config_version"] == 1
    assert profile["control_plane"]["base_url"] == "https://api.openai.com"
    assert profile["control_plane"]["api_key"] == "env:CONTROL_PLANE_API_KEY"
    assert "tunnel_id" not in profile["control_plane"]
    assert profile["health"] == {
        "listen_addr": "127.0.0.1:8080",
        "url_file": str(health_url.resolve()),
    }
    assert profile["admin_ui"] == {"open_browser": False}
    assert profile["mcp"]["commands"] == [{
        "channel": "main",
        "command": f'"{python_executable.resolve()}" -m bridge.mcp.server --config "{bridge_config.resolve()}"',
    }]
    validate_profile(profile)


@pytest.mark.parametrize("value", ["", "tunnel_bad", "tunnel_ABC", "tunnel_0123"])
def test_validate_tunnel_id_rejects_invalid_values(value):
    with pytest.raises(TunnelProfileError):
        validate_tunnel_id(value)


def test_validate_environment_fails_closed_without_runtime_credentials():
    with pytest.raises(TunnelProfileError, match="CONTROL_PLANE_API_KEY"):
        validate_environment({})


def test_validate_environment_does_not_echo_runtime_key():
    secret = "sk-test-value"
    with pytest.raises(TunnelProfileError) as exc_info:
        validate_environment({"CONTROL_PLANE_API_KEY": secret})
    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("401 unauthorized from control plane", "TUNNEL_AUTH"),
        ("failed to start MCP child command", "LOCAL_MCP_START"),
        ("initialize request failed", "MCP_INITIALIZE"),
        ("tools/list discovery failed", "TOOLS_DISCOVERY"),
        ("codex app-server discovery failed", "CODEX_DISCOVERY"),
        ("poll timed out contacting control plane", "CONTROL_PLANE"),
    ],
)
def test_classify_doctor_failure_returns_required_layer(text, expected):
    assert classify_doctor_failure(text) == expected


def test_sanitize_diagnostics_redacts_supplied_secret():
    assert sanitize_diagnostics("key=sk-test-value", ["sk-test-value"]) == "key=[redacted]"
~~~

- [ ] Step 2: Run the focused tests and verify the expected red failure.

Run:

~~~powershell
pytest tests/unit/test_tunnel_profile.py -q
~~~

Expected: collection fails specifically because bridge.tunnel_profile does not exist. Fix only test typos if the failure is unrelated.

## Task 2: Implement the pure profile module

**Files:**
- Create: bridge/tunnel_profile.py
- Test: tests/unit/test_tunnel_profile.py

- [ ] Step 1: Implement the minimal tested API.

Implement these fixed interfaces:

~~~python
class TunnelProfileError(ValueError):
    """Safe local profile or preflight validation failure."""


def build_profile(python_executable, bridge_config, health_url_file) -> dict:
    ...


def validate_profile(profile) -> None:
    ...


def validate_profile_file(path) -> dict:
    ...


def validate_tunnel_id(value: str) -> str:
    ...


def validate_environment(environment) -> None:
    ...


def sanitize_diagnostics(text: str, secret_values=()) -> str:
    ...


def classify_doctor_failure(text: str) -> str:
    ...
~~~

Use yaml.safe_load and yaml.safe_dump only for profile files. Do not import supervisors, queues, runners, Codex modules, or transports. Generate exactly one mcp.commands item with channel main; quote both absolute Windows paths in the command. Require tunnel_ followed by 32 lowercase hexadecimal characters. Keep errors generic and never echo key values.

validate_profile must enforce config_version 1, the minimum generated-profile field whitelist, control_plane.api_key exactly env:CONTROL_PLANE_API_KEY, no control_plane.tunnel_id, loopback health listener, top-level admin_ui.open_browser false, no mcp.server_urls, exactly one main command, and the command form:

    "absolute-python-path" -m bridge.mcp.server --config "absolute-config-path"

Reject literal fields whose names indicate API keys, admin keys, bearer tokens, or tunnel tokens anywhere in the profile mapping. validate_environment requires non-empty CONTROL_PLANE_API_KEY and a valid CONTROL_PLANE_TUNNEL_ID but returns neither value. sanitize_diagnostics replaces supplied secrets, Authorization Bearer values, and obvious key-shaped values with [redacted]. classify_doctor_failure must map auth markers first, then local child start, MCP initialize, tools discovery, Codex discovery, and otherwise CONTROL_PLANE.

Provide a fixed CLI with only:

    python -m bridge.tunnel_profile generate --python-executable PATH --config-path PATH --profile-path PATH
    python -m bridge.tunnel_profile validate --profile-path PATH
    python -m bridge.tunnel_profile classify

generate validates absolute path shape, creates the profile parent, writes YAML, validates the result, and prints only a non-sensitive success line. validate reads and validates a profile. classify reads diagnostics from stdin and prints one layer. Do not add subprocess, shell, arbitrary Python-expression, or network behavior.

- [ ] Step 2: Run focused tests and verify green.

    pytest tests/unit/test_tunnel_profile.py -q

Expected: all profile tests pass.

- [ ] Step 3: Test the module CLI with safe temporary paths.

Generate a profile using temporary fake absolute paths and run validate on that generated file. Do not read config.local.yaml and do not provide credentials.

    python -m bridge.tunnel_profile validate --profile-path .bridge/tunnel-profile.yaml

Expected before generation: a safe nonzero missing-file error, with no traceback or secret echo.

- [ ] Step 4: Commit the module and tests.

    git add bridge/tunnel_profile.py tests/unit/test_tunnel_profile.py
    git commit -m "feat: add secure tunnel profile validation"

## Task 3: Add fail-closed PowerShell wrappers

**Files:**
- Create: scripts/tunnel_profile.ps1
- Create: scripts/tunnel_doctor.ps1
- Create: scripts/tunnel_start.ps1
- Test: tests/unit/test_tunnel_profile.py for any added Python helper behavior

- [ ] Step 1: Add tests first for any new helper behavior.

Add tests for valid/invalid environment preflight, the exact classify stdin contract, and redaction. Run the focused test file and verify new tests fail for missing behavior before implementation.

- [ ] Step 2: Implement scripts/tunnel_profile.ps1.

Use Set-StrictMode -Version Latest and $ErrorActionPreference = 'Stop'. Resolve repository root from $PSScriptRoot\..; default profile to the repository-root path .bridge\tunnel-profile.yaml, config to the repository-root path config.local.yaml, and Python to $env:BRIDGE_MCP_PYTHON or python. Accept explicit -PythonExecutable, -ConfigPath, and -ProfilePath. Check executable and config before invoking the fixed module command:

    & $PythonExecutable -m bridge.tunnel_profile generate --python-executable $PythonExecutable --config-path $ConfigPath --profile-path $ProfilePath

Never accept or forward an API key/admin key parameter.

- [ ] Step 3: Implement scripts/tunnel_doctor.ps1.

Accept -TunnelClientPath (default tunnel-client), -PythonExecutable, and -ProfilePath. Resolve the official executable with Get-Command or an explicit existing path. Before any remote command, fail if the profile is missing, Python/config validation fails, CONTROL_PLANE_API_KEY is absent, or CONTROL_PLANE_TUNNEL_ID is absent/invalid. Report only SET/UNSET for variables.

Invoke only:

    & $TunnelClientPath doctor --profile-file $ProfilePath --explain

Capture combined output, pass it through the fixed module classify command, sanitize before display, print FAILURE_LAYER= followed by the selected layer on nonzero exit, and preserve the official exit code. Use TUNNEL_AUTH for missing/invalid runtime credentials and LOCAL_MCP_START for local profile/child failures. Do not print environment values.

- [ ] Step 4: Implement scripts/tunnel_start.ps1.

Use the same parameters and preflight. Invoke the doctor script first; stop on nonzero status. Then run only the official foreground command:

    & $TunnelClientPath run --profile-file $ProfilePath
    exit $LASTEXITCODE

Do not use Start-Process, create a listener, create a tunnel, use OPENAI_ADMIN_KEY, or implement a replacement daemon.

- [ ] Step 5: Run safe negative paths.

Run scripts with missing variables/profile and verify they fail before trying a network command. Use only fake paths and a test secret string; verify output contains failure layer/UNSET but not the test secret.

- [ ] Step 6: Commit wrappers.

    git add scripts/tunnel_profile.ps1 scripts/tunnel_doctor.ps1 scripts/tunnel_start.ps1
    git commit -m "feat: add fail-closed tunnel operators"

## Task 4: Add a default-skipped opt-in doctor smoke

**Files:**
- Create: tests/smoke/test_secure_mcp_tunnel.py

- [ ] Step 1: Write the guarded test.

Skip unless RUN_SECURE_MCP_TUNNEL_SMOKE == "1". When enabled, require TUNNEL_CLIENT_PATH, TUNNEL_PROFILE_PATH, CONTROL_PLANE_API_KEY, and CONTROL_PLANE_TUNNEL_ID to already exist, invoke only tunnel-client doctor --profile-file followed by the profile path and --explain, assert exit code 0, and avoid printing captured output or secrets. Use subprocess only for this explicit fixed executable/argument list; never use shell=True.

- [ ] Step 2: Run without opt-in.

    pytest tests/smoke/test_secure_mcp_tunnel.py -q

Expected: one skipped test and no process/network invocation.

- [ ] Step 3: Commit the smoke entry.

    git add tests/smoke/test_secure_mcp_tunnel.py
    git commit -m "test: add opt-in secure tunnel doctor smoke"

## Task 5: Write the runbook and correct existing MCP docs

**Files:**
- Create: docs/chatgpt-web-tunnel.md
- Modify: docs/mcp-client-setup.md
- Modify: docs/mcp-tools.md
- Modify: README.md

- [ ] Step 1: Write docs/chatgpt-web-tunnel.md.

State the exact paths:

    Compatible local MCP client -> STDIO -> Bridge
    ChatGPT Web -> OpenAI Secure MCP Tunnel -> tunnel-client -> STDIO -> Bridge

Document official installation sources, tunnel-client --version, tunnel-client help quickstart, the repository profile generator, tunnel_doctor.ps1, tunnel_start.ps1, and local /healthz, /readyz, /ui checks. Explain that the official profile uses config_version 1, control_plane.api_key env:CONTROL_PLANE_API_KEY, and mcp.commands channel main.

Explain key handling: CONTROL_PLANE_API_KEY is the runtime key for doctor/run; OPENAI_ADMIN_KEY is only for admin tunnel CRUD; neither is stored in repo/profile or passed as a CLI argument. Give PowerShell session setup/cleanup using placeholders and a secure prompt, never a real credential.

Document deferred Part 2 commands: obtain/reuse a tunnel ID, install the official binary, set runtime env, generate profile, doctor, start, verify health/readiness/UI, configure ChatGPT Web Settings -> Connectors/Apps -> Connection: Tunnel, discover nine tools, run bridge_list_projects, a small read-only code task, status/events/artifacts, accept, steer, and interrupt. Mark all account-level and Web UI actions as unexecuted in Part 1. Include the official rule to keep one active stdio tunnel-client per tunnel ID.

- [ ] Step 2: Correct docs/mcp-client-setup.md.

Replace claims that ChatGPT Desktop directly starts the local Python STDIO server with:

    Compatible local MCP clients may launch the STDIO server directly.
    ChatGPT Web reaches the local STDIO MCP server through OpenAI Secure MCP Tunnel.

Keep existing local STDIO instructions and link the new runbook.

- [ ] Step 3: Correct docs/mcp-tools.md.

Replace the ChatGPT Desktop-only setup link with compatible local MCP client wording and a link to docs/chatgpt-web-tunnel.md for ChatGPT Web through Secure MCP Tunnel.

- [ ] Step 4: Add one README runbook link.

Do not rewrite runtime behavior and do not claim that a live tunnel is configured.

- [ ] Step 5: Commit documentation.

    git add docs/chatgpt-web-tunnel.md docs/mcp-client-setup.md README.md
    git commit -m "docs: document ChatGPT Web secure MCP tunnel"

## Task 6: Full verification and scope audit

**Files:** read-only verification of all changed files and Git state

- [ ] Step 1: Run focused tests.

    pytest tests/unit/test_tunnel_profile.py tests/smoke/test_secure_mcp_tunnel.py -q

Expected: all focused unit tests pass and the opt-in smoke is skipped.

- [ ] Step 2: Run the complete ordinary suite.

    pytest -q

Expected: zero failures; no test requires tunnel-client, OpenAI credentials, control-plane access, or ChatGPT Web.

- [ ] Step 3: Verify safety and untouched runtime.

    git diff --check
    git status --short
    git diff --name-only f933bc3..HEAD
    rg -n "TaskSupervisor|WorkerQueue|TaskRunner|CodexSessionManager|worktree" bridge/orchestration bridge/codex bridge/worktree.py

Confirm only planned files changed after f933bc3, .bridge/ remains ignored, no key-like literal or generated profile is tracked, and no runtime/source file was modified.

- [ ] Step 4: Report evidence and unverified acceptance items.

Report implementation commits, changed files, profile shape, secret handling, script commands, exact pytest counts, and explicitly state that these remain unverified because Part 2 is deferred: tunnel-client version/path, control-plane reachability, real tunnel metadata, successful doctor/run, live health/readiness/UI, ChatGPT Web discovery, real bridge_list_projects result, Codex task reaching WAITING_REVIEW, status/events/artifacts/accept, steer, interrupt, and completion protection.
