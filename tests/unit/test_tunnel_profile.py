from __future__ import annotations

from copy import deepcopy

import pytest

from bridge.tunnel_profile import (
    TunnelProfileError,
    build_profile,
    classify_doctor_failure,
    sanitize_diagnostics,
    validate_environment,
    validate_profile,
    validate_profile_file,
    validate_tunnel_id,
)


def _profile(tmp_path):
    return build_profile(
        tmp_path / "Python 3.9" / "python.exe",
        tmp_path / "AI project bridge" / "config.local.yaml",
        tmp_path / ".bridge" / "tunnel-health.url",
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
    assert profile["mcp"]["commands"] == [
        {
            "channel": "main",
            "command": f'"{python_executable.resolve()}" -m bridge.mcp.server --config "{bridge_config.resolve()}"',
        }
    ]
    validate_profile(profile)


def test_validate_profile_file_round_trips_generated_yaml(tmp_path):
    import yaml

    profile_path = tmp_path / ".bridge" / "tunnel-profile.yaml"
    profile_path.parent.mkdir()
    profile_path.write_text(yaml.safe_dump(_profile(tmp_path), sort_keys=False), encoding="utf-8")

    assert validate_profile_file(profile_path)["config_version"] == 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda profile: profile["control_plane"].update(api_key="sk-test-value"), "api_key"),
        (lambda profile: profile["control_plane"].update(tunnel_id="tunnel_" + "0" * 32), "tunnel_id"),
        (lambda profile: profile["mcp"].update(server_urls=[]), "server_urls"),
        (lambda profile: profile["mcp"]["commands"].__setitem__(0, {"channel": "tools", "command": "python -m bridge.mcp.server --config C:/config.yaml"}), "channel"),
        (lambda profile: profile["mcp"]["commands"].__setitem__(0, {"channel": "main", "command": "python -m bridge.mcp.server"}), "--config"),
    ],
)
def test_validate_profile_rejects_unsafe_or_incomplete_variants(tmp_path, mutate, message):
    profile = deepcopy(_profile(tmp_path))
    mutate(profile)

    with pytest.raises(TunnelProfileError, match=message):
        validate_profile(profile)


def test_validate_profile_rejects_duplicate_main_commands(tmp_path):
    profile = _profile(tmp_path)
    profile["mcp"]["commands"].append(deepcopy(profile["mcp"]["commands"][0]))

    with pytest.raises(TunnelProfileError, match="exactly one"):
        validate_profile(profile)


def test_validate_profile_rejects_unapproved_optional_fields(tmp_path):
    profile = _profile(tmp_path)
    profile["mcp"]["extra_headers"] = {
        "Authorization": "Bearer runtime-token-placeholder",
    }

    with pytest.raises(TunnelProfileError, match="unsupported"):
        validate_profile(profile)


@pytest.mark.parametrize("path_name", ["python.exe", "config.local.yaml", "health.url"])
def test_build_profile_requires_absolute_paths(path_name):
    paths = {
        "python.exe": "python.exe",
        "config.local.yaml": "config.local.yaml",
        "health.url": "health.url",
    }
    with pytest.raises(TunnelProfileError, match="absolute"):
        build_profile(paths["python.exe"], paths["config.local.yaml"], paths["health.url"])


@pytest.mark.parametrize("value", ["", "tunnel_bad", "tunnel_ABC", "tunnel_0123", "tunnel_" + "0" * 31])
def test_validate_tunnel_id_rejects_invalid_values(value):
    with pytest.raises(TunnelProfileError):
        validate_tunnel_id(value)


def test_validate_tunnel_id_accepts_official_shape():
    value = "tunnel_" + "0123456789abcdef" * 2
    assert validate_tunnel_id(value) == value


def test_validate_environment_fails_closed_without_runtime_credentials():
    with pytest.raises(TunnelProfileError, match="CONTROL_PLANE_API_KEY"):
        validate_environment({})

    with pytest.raises(TunnelProfileError, match="CONTROL_PLANE_TUNNEL_ID"):
        validate_environment({"CONTROL_PLANE_API_KEY": "placeholder-runtime-key"})


def test_validate_environment_does_not_echo_runtime_key():
    secret = "sk-test-value"
    with pytest.raises(TunnelProfileError) as exc_info:
        validate_environment({"CONTROL_PLANE_API_KEY": secret})
    assert secret not in str(exc_info.value)


def test_sanitize_diagnostics_redacts_supplied_and_key_shaped_values():
    text = "key=sk-test-value bearer=sk-proj-abcdefghijklmnopqrstuvwxyz"

    sanitized = sanitize_diagnostics(text, ["sk-test-value"])

    assert "sk-test-value" not in sanitized
    assert "sk-proj-abcdefghijklmnopqrstuvwxyz" not in sanitized
    assert sanitized == "key=[redacted] bearer=[redacted]"


def test_sanitize_diagnostics_redacts_authorization_bearer_value():
    text = "Authorization: Bearer runtime-token-placeholder"

    sanitized = sanitize_diagnostics(text)

    assert sanitized == "Authorization: Bearer [redacted]"


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


def test_auth_classification_takes_precedence_over_generic_mcp_text():
    assert classify_doctor_failure("MCP initialize returned 403 forbidden") == "TUNNEL_AUTH"
