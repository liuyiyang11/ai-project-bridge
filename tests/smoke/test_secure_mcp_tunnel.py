from __future__ import annotations

import os
import subprocess

import pytest

from bridge.tunnel_profile import validate_tunnel_id


@pytest.mark.skipif(
    os.environ.get("RUN_SECURE_MCP_TUNNEL_SMOKE") != "1",
    reason="secure MCP tunnel smoke is opt-in: set RUN_SECURE_MCP_TUNNEL_SMOKE=1",
)
def test_opt_in_secure_mcp_tunnel_doctor():
    required = {
        "TUNNEL_CLIENT_PATH": os.environ.get("TUNNEL_CLIENT_PATH"),
        "TUNNEL_PROFILE_PATH": os.environ.get("TUNNEL_PROFILE_PATH"),
        "CONTROL_PLANE_API_KEY": os.environ.get("CONTROL_PLANE_API_KEY"),
        "CONTROL_PLANE_TUNNEL_ID": os.environ.get("CONTROL_PLANE_TUNNEL_ID"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        pytest.fail("opt-in smoke requires: " + ", ".join(missing))

    validate_tunnel_id(required["CONTROL_PLANE_TUNNEL_ID"])
    result = subprocess.run(
        [
            required["TUNNEL_CLIENT_PATH"],
            "doctor",
            "--profile-file",
            required["TUNNEL_PROFILE_PATH"],
            "--explain",
        ],
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        timeout=120,
    )

    assert result.returncode == 0, "opt-in tunnel-client doctor failed"
