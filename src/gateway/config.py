import re
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _require_yaml_module():
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError("PyYAML is required for gateway config loading.") from exc
    return yaml


_DURATION_RE = re.compile(r"^(\d+)([smhd])$")
_ENV_VAR_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")


def parse_duration_to_seconds(value: str) -> int:
    match = _DURATION_RE.match((value or "").strip())
    if not match:
        raise ValueError(f"Invalid duration format: {value!r}")

    amount = int(match.group(1))
    unit = match.group(2)
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return amount * factors[unit]


@dataclass
class ChannelConfig:
    enabled: bool
    bot_token: str | None = None
    dm_policy: str = "pairing"


@dataclass
class GatewayConfig:
    request_timeout: int
    session_ttl_dm: int
    session_ttl_group: int
    approval_wait_timeout: int
    channels: dict[str, ChannelConfig]


def _validate_channel(name: str, raw: dict[str, Any]) -> ChannelConfig:
    enabled = raw.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError(f"channels.{name}.enabled must be boolean")

    token = raw.get("bot_token")
    if isinstance(token, str):
        token = _resolve_env_value(token)
    dm_policy = raw.get("dm_policy", "pairing")
    if dm_policy not in {"pairing", "open", "deny"}:
        raise ValueError(f"channels.{name}.dm_policy must be one of pairing/open/deny")

    if enabled and not token:
        raise ValueError(f"channels.{name}.bot_token is required when enabled=true")

    return ChannelConfig(enabled=enabled, bot_token=token, dm_policy=dm_policy)


def _resolve_env_value(value: str) -> str:
    text = (value or "").strip()
    match = _ENV_VAR_RE.match(text)
    if not match:
        return text

    env_name = match.group(1)
    env_val = os.getenv(env_name)
    if not env_val:
        raise ValueError(f"Environment variable {env_name} is required but missing")
    return env_val


def load_gateway_config(path: str | Path) -> GatewayConfig:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except Exception:
        pass

    yaml = _require_yaml_module()

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Gateway config not found: {config_path}")

    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Gateway config must be a YAML object")

    gateway = data.get("gateway")
    channels = data.get("channels")
    if not isinstance(gateway, dict):
        raise ValueError("gateway section is required")
    if not isinstance(channels, dict):
        raise ValueError("channels section is required")

    request_timeout = parse_duration_to_seconds(gateway.get("request_timeout", "30s"))
    session_ttl_dm = parse_duration_to_seconds(gateway.get("session_ttl_dm", "24h"))
    session_ttl_group = parse_duration_to_seconds(gateway.get("session_ttl_group", "8h"))
    approval_wait_timeout = parse_duration_to_seconds(gateway.get("approval_wait_timeout", "24h"))

    channel_cfg: dict[str, ChannelConfig] = {}
    for name, raw in channels.items():
        if not isinstance(raw, dict):
            raise ValueError(f"channels.{name} must be an object")
        channel_cfg[name] = _validate_channel(name, raw)

    return GatewayConfig(
        request_timeout=request_timeout,
        session_ttl_dm=session_ttl_dm,
        session_ttl_group=session_ttl_group,
        approval_wait_timeout=approval_wait_timeout,
        channels=channel_cfg,
    )
