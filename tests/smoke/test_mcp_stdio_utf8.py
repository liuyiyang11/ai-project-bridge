from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

import bridge.mcp.server as mcp_server


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_stdio_configuration_is_explicit_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    streams = {
        "stdin": io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="surrogateescape"),
        "stdout": io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="surrogateescape"),
        "stderr": io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="surrogateescape"),
    }
    try:
        for name, stream in streams.items():
            monkeypatch.setattr(mcp_server.sys, name, stream)

        mcp_server._configure_stdio_utf8()

        assert streams["stdin"].encoding.lower().replace("-", "") == "utf8"
        assert streams["stdin"].errors == "strict"
        assert streams["stdout"].encoding.lower().replace("-", "") == "utf8"
        assert streams["stdout"].errors == "strict"
        assert streams["stderr"].encoding.lower().replace("-", "") == "utf8"
        assert streams["stderr"].errors == "backslashreplace"
    finally:
        for stream in streams.values():
            stream.close()


def test_stdio_configuration_rejects_non_reconfigurable_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server.sys, "stdin", object())

    with pytest.raises(RuntimeError, match="stdin stream does not support UTF-8 configuration"):
        mcp_server._configure_stdio_utf8()


def _request_payload(instruction: str, acceptance: list[str]) -> bytes:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "utf8-regression", "version": "test"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "bridge_start_code_task",
                "arguments": {
                    "project": "demo",
                    "instruction": instruction,
                    "acceptance": acceptance,
                },
            },
        },
    ]
    # This must remain a real UTF-8 wire payload. Do not use ensure_ascii=True.
    return ("\n".join(json.dumps(message, ensure_ascii=False) for message in messages) + "\n").encode("utf-8")


def _run_mcp_server(tmp_path: Path, instruction: str, acceptance: list[str]) -> tuple[list[dict], bytes, bytes]:
    config_path = tmp_path / "config.local.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "control_repo": "owner/bridge",
                "trusted_github_logins": ["trusted-user"],
                "codex": {"backend": "app-server"},
                "projects": {
                    "demo": {
                        "capabilities": ["code"],
                        "root": str(tmp_path),
                        "repo": "owner/demo",
                        "allowed_commands": {},
                        "artifact_dirs": [],
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONIOENCODING": "gbk:surrogateescape",
            "PYTHONUTF8": "0",
            "PYTHONPATH": str(REPOSITORY_ROOT) + os.pathsep + environment.get("PYTHONPATH", ""),
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "bridge.mcp.server", "--config", str(config_path)],
        cwd=REPOSITORY_ROOT,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    stdout, stderr = process.communicate(_request_payload(instruction, acceptance), timeout=15)
    assert process.returncode == 0, stderr.decode("utf-8", errors="replace")
    responses = [json.loads(line.decode("utf-8")) for line in stdout.splitlines() if line.strip()]
    return responses, stdout, stderr


@pytest.mark.parametrize(
    ("label", "instruction", "acceptance"),
    [
        ("ascii", "Read README and return one sentence.", ["Do not modify any files."]),
        (
            "chinese",
            "读取 README 第一段并返回一句概括，不修改任何文件。",
            ["不得修改、创建或删除任何文件。"],
        ),
        ("emoji", "Read README 😀 and return one sentence.", ["Do not modify any files."]),
        ("u_fffd", "Read README � and return one sentence.", ["Do not modify any files."]),
    ],
)
def test_raw_utf8_mcp_stdio_tool_call_survives_gbk_child(
    tmp_path: Path,
    label: str,
    instruction: str,
    acceptance: list[str],
) -> None:
    responses, stdout, _ = _run_mcp_server(tmp_path, instruction, acceptance)
    assert all(isinstance(response, dict) for response in responses), label
    tool_response = next(response for response in responses if response.get("id") == 2)
    result = tool_response["result"]
    assert result["isError"] is False, (label, stdout.decode("utf-8", errors="replace"))
    assert result["structuredContent"]["state"] == "QUEUED"
    assert result["structuredContent"]["project"] == "demo"
    task_files = list((tmp_path / ".bridge" / "tasks").glob("*/task.json"))
    assert len(task_files) == 1
    task = json.loads(task_files[0].read_text(encoding="utf-8"))
    assert task["instruction"] == instruction
    assert task["acceptance"] == acceptance
