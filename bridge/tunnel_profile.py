from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

import yaml


class TunnelProfileError(ValueError):
    """Safe local profile or preflight validation failure."""


_TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")
_COMMAND_RE = re.compile(
    r'^\s*"(?P<python>[^"]+)"\s+-m\s+bridge\.mcp\.server\s+'
    r'--config\s+"(?P<config>[^"]+)"\s*$'
)
_SECRET_FIELD_RE = re.compile(r"(?:api[_-]?key|admin[_-]?key|bearer|token)", re.IGNORECASE)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|admin[_-]?key|bearer|token)\b\s*[:=]\s*)[^\s,;]+"
)
_AUTHORIZATION_BEARER_RE = re.compile(
    r"(?i)(\bAuthorization\b\s*:\s*Bearer\s+)[^\s,;]+"
)
_AUTHORIZATION_ASSIGNMENT_RE = re.compile(
    r"(?i)(\bAuthorization\b\s*[:=]\s*)(?!Bearer\b)[^\s,;]+"
)
_BEARER_VALUE_RE = re.compile(r"(?i)(\bBearer\s+)[^\s,;]+")
_OBVIOUS_SECRET_RE = re.compile(r"(?i)\bsk-[A-Za-z0-9_-]{8,}\b")


def _absolute_path(value: Union[str, Path], field: str) -> Path:
    try:
        path = Path(value).expanduser()
    except (TypeError, ValueError) as exc:
        raise TunnelProfileError(f"{field} must be an absolute path") from exc
    if not path.is_absolute() or '"' in str(path):
        raise TunnelProfileError(f"{field} must be an absolute path")
    return path.resolve(strict=False)


def build_profile(
    python_executable: Union[str, Path],
    bridge_config: Union[str, Path],
    health_url_file: Union[str, Path],
) -> dict[str, Any]:
    """Build the official tunnel-client YAML shape for Bridge's STDIO MCP."""
    python_path = _absolute_path(python_executable, "python executable")
    config_path = _absolute_path(bridge_config, "Bridge config")
    health_path = _absolute_path(health_url_file, "health URL file")
    return {
        "config_version": 1,
        "control_plane": {
            "base_url": "https://api.openai.com",
            "api_key": "env:CONTROL_PLANE_API_KEY",
        },
        "health": {
            "listen_addr": "127.0.0.1:8080",
            "url_file": str(health_path),
        },
        "admin_ui": {"open_browser": False},
        "mcp": {
            "commands": [
                {
                    "channel": "main",
                    "command": f'"{python_path}" -m bridge.mcp.server --config "{config_path}"',
                }
            ]
        },
    }


def _reject_secret_fields(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            name = str(key)
            normalized_path = path + (name,)
            if _SECRET_FIELD_RE.search(name):
                if normalized_path == ("control_plane", "api_key"):
                    if child != "env:CONTROL_PLANE_API_KEY":
                        raise TunnelProfileError("control_plane.api_key must use the runtime environment reference")
                else:
                    raise TunnelProfileError(f"profile contains forbidden secret field: {name}")
            _reject_secret_fields(child, normalized_path)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_fields(child, path)


def _ensure_allowed_fields(value: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in value:
        if key not in allowed:
            raise TunnelProfileError(f"unsupported profile field: {path}.{key}")


def validate_profile(profile: Mapping[str, Any]) -> None:
    """Validate the Bridge-specific subset of tunnel-client's profile schema."""
    if not isinstance(profile, Mapping):
        raise TunnelProfileError("profile must be a YAML mapping")
    if profile.get("config_version") != 1:
        raise TunnelProfileError("profile config_version must be 1")

    _ensure_allowed_fields(profile, {"config_version", "control_plane", "health", "admin_ui", "mcp"}, "profile")
    _reject_secret_fields(profile)

    control_plane = profile.get("control_plane")
    if not isinstance(control_plane, Mapping):
        raise TunnelProfileError("profile control_plane is required")
    _ensure_allowed_fields(control_plane, {"base_url", "api_key"}, "control_plane")
    if control_plane.get("base_url") != "https://api.openai.com":
        raise TunnelProfileError("profile control_plane.base_url must be https://api.openai.com")
    if control_plane.get("api_key") != "env:CONTROL_PLANE_API_KEY":
        raise TunnelProfileError("control_plane.api_key must use the runtime environment reference")
    if "tunnel_id" in control_plane:
        raise TunnelProfileError("control_plane.tunnel_id must come from CONTROL_PLANE_TUNNEL_ID")

    health = profile.get("health")
    if not isinstance(health, Mapping):
        raise TunnelProfileError("profile health is required")
    _ensure_allowed_fields(health, {"listen_addr", "url_file"}, "health")
    if health.get("listen_addr") != "127.0.0.1:8080":
        raise TunnelProfileError("health listener must remain on 127.0.0.1:8080")
    url_file = health.get("url_file")
    if not isinstance(url_file, str):
        raise TunnelProfileError("health.url_file must be an absolute path")
    _absolute_path(url_file, "health.url_file")

    admin_ui = profile.get("admin_ui")
    if not isinstance(admin_ui, Mapping) or admin_ui.get("open_browser") is not False:
        raise TunnelProfileError("admin_ui.open_browser must be false")
    _ensure_allowed_fields(admin_ui, {"open_browser"}, "admin_ui")

    mcp = profile.get("mcp")
    if not isinstance(mcp, Mapping):
        raise TunnelProfileError("profile mcp is required")
    _ensure_allowed_fields(mcp, {"commands"}, "mcp")
    if "server_urls" in mcp:
        raise TunnelProfileError("HTTP MCP server_urls are not allowed")
    commands = mcp.get("commands")
    if not isinstance(commands, list) or len(commands) != 1:
        raise TunnelProfileError("profile must contain exactly one MCP command")
    command = commands[0]
    if not isinstance(command, Mapping) or command.get("channel") != "main":
        raise TunnelProfileError("profile MCP command channel must be main")
    _ensure_allowed_fields(command, {"channel", "command"}, "mcp.commands[0]")
    command_text = command.get("command")
    if not isinstance(command_text, str):
        raise TunnelProfileError("MCP command must include --config")
    match = _COMMAND_RE.fullmatch(command_text)
    if match is None:
        raise TunnelProfileError("MCP command must be quoted and include bridge.mcp.server and --config")
    _absolute_path(match.group("python"), "MCP Python executable")
    _absolute_path(match.group("config"), "MCP Bridge config")


def validate_profile_file(path: Union[str, Path]) -> dict[str, Any]:
    """Load and validate a YAML profile without reflecting its contents."""
    profile_path = _absolute_path(path, "profile path")
    try:
        raw = profile_path.read_text(encoding="utf-8")
        profile = yaml.safe_load(raw)
    except FileNotFoundError as exc:
        raise TunnelProfileError("profile file does not exist") from exc
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise TunnelProfileError("profile file could not be read as YAML") from exc
    validate_profile(profile)
    return dict(profile)


def validate_tunnel_id(value: str) -> str:
    if not isinstance(value, str) or _TUNNEL_ID_RE.fullmatch(value) is None:
        raise TunnelProfileError("CONTROL_PLANE_TUNNEL_ID must match tunnel_ followed by 32 lowercase hexadecimal characters")
    return value


def validate_environment(environment: Mapping[str, str]) -> None:
    """Validate required runtime environment values without returning secrets."""
    api_key = environment.get("CONTROL_PLANE_API_KEY") if isinstance(environment, Mapping) else None
    if not isinstance(api_key, str) or not api_key.strip():
        raise TunnelProfileError("CONTROL_PLANE_API_KEY is required")
    tunnel_id = environment.get("CONTROL_PLANE_TUNNEL_ID")
    if not isinstance(tunnel_id, str) or not tunnel_id.strip():
        raise TunnelProfileError("CONTROL_PLANE_TUNNEL_ID is required")
    validate_tunnel_id(tunnel_id)


def sanitize_diagnostics(text: str, secret_values: Iterable[str] = ()) -> str:
    value = str(text)
    for secret in secret_values:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "[redacted]")
    value = _AUTHORIZATION_BEARER_RE.sub(r"\1[redacted]", value)
    value = _AUTHORIZATION_ASSIGNMENT_RE.sub(r"\1[redacted]", value)
    value = _SECRET_ASSIGNMENT_RE.sub(r"\1[redacted]", value)
    value = _BEARER_VALUE_RE.sub(r"\1[redacted]", value)
    value = _OBVIOUS_SECRET_RE.sub("[redacted]", value)
    return value[:12000]


def classify_doctor_failure(text: str) -> str:
    value = str(text).lower()
    if re.search(r"\b(?:401|403)\b|unauthori[sz]ed|forbidden|api[_ -]?key|tunnel[_ -]?id.*required", value):
        return "TUNNEL_AUTH"
    if re.search(r"mcp (?:child|server)|child process|failed to start|spawn|executable|command not found", value):
        return "LOCAL_MCP_START"
    if re.search(r"initialize|initialise", value):
        return "MCP_INITIALIZE"
    if re.search(r"tools/list|tool discovery|tools discovery", value):
        return "TOOLS_DISCOVERY"
    if re.search(r"codex|app-server", value):
        return "CODEX_DISCOVERY"
    return "CONTROL_PLANE"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m bridge.tunnel_profile")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--python-executable", required=True)
    generate.add_argument("--config-path", required=True)
    generate.add_argument("--profile-path", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("--profile-path", required=True)

    subparsers.add_parser("classify")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "generate":
            profile_path = _absolute_path(args.profile_path, "profile path")
            profile = build_profile(
                args.python_executable,
                args.config_path,
                profile_path.parent / "tunnel-health.url",
            )
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            profile_path.write_text(yaml.safe_dump(profile, sort_keys=False), encoding="utf-8")
            validate_profile(profile)
            print("Tunnel profile generated")
            return 0
        if args.command == "validate":
            validate_profile_file(args.profile_path)
            print("Tunnel profile valid")
            return 0
        print(classify_doctor_failure(sys.stdin.read()))
        return 0
    except (OSError, TunnelProfileError):
        print("Tunnel profile operation failed", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
