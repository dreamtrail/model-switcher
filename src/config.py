"""YAML configuration loader with ${PORT} substitution."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_PORT_PLACEHOLDER = "${PORT}"
_PORT_RE = re.compile(r"\$\{PORT\}")
_DETECT_PORT_RE = re.compile(r"(?<!\w)--port\s+[\"']?(\d+)[\"']?")


@dataclass
class ModelConfig:
    """Configuration for a single model."""

    name: str
    cmd: str  # command (possibly with ${PORT} substituted)
    port: int = 0  # resolved port
    _explicit_port: int | None = None  # user-specified port from config


@dataclass
class Config:
    """Parsed configuration."""

    models: dict[str, ModelConfig] = field(default_factory=dict)


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _detect_port_from_cmd(cmd: str) -> int | None:
    """Try to detect a port number from --port flags in a command string.

    Matches patterns like --port 8000, --port "8000", --port '8000'.
    Returns the first match found, or None if no --port flag is present.
    """
    match = _DETECT_PORT_RE.search(cmd)
    if match:
        return int(match.group(1))
    return None


def _resolve_ports(models: dict[str, ModelConfig]) -> None:
    """Resolve ports for each model using three-tier resolution.

    Priority (first match wins):
    1. ${PORT} placeholder — auto-assign free port, substitute into cmd
    2. Explicit port from config — use as-is
    3. Auto-detect --port flag from cmd string
    4. Raise ValueError if none of the above
    """
    for name, mc in models.items():
        # Tier 1: ${PORT} placeholder — auto-assign
        if _PORT_PLACEHOLDER in mc.cmd:
            port = _find_free_port()
            mc.port = port
            mc.cmd = _PORT_RE.sub(str(port), mc.cmd)
        # Tier 2: Explicit port from config
        elif mc._explicit_port is not None:
            port = mc._explicit_port
            if not (1 <= port <= 65535):
                raise ValueError(
                    f"Model '{name}' port {port} is out of range (1-65535)"
                )
            mc.port = port
        # Tier 3: Auto-detect from --port flag
        else:
            detected = _detect_port_from_cmd(mc.cmd)
            if detected is not None:
                if not (1 <= detected <= 65535):
                    raise ValueError(
                        f"Model '{name}' detected port {detected} "
                        f"is out of range (1-65535)"
                    )
                mc.port = detected
            else:
                raise ValueError(
                    f"Model '{name}' has no port: command must contain "
                    f"'{_PORT_PLACEHOLDER}', an explicit 'port:' key, "
                    f"or a '--port <N>' flag"
                )


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML configuration file.

    Raises:
        FileNotFoundError: if path does not exist.
        ValueError: if config is malformed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = path.read_text()
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise ValueError(
            f"Config must be a YAML mapping, got {type(data).__name__}"
        )

    models: dict[str, ModelConfig] = {}
    for name, value in data.items():
        if isinstance(value, str):
            cmd = value
        elif isinstance(value, dict):
            cmd = value.get("cmd", "")
        else:
            raise ValueError(
                f"Model '{name}' value must be a string or mapping with 'cmd', "
                f"got {type(value).__name__}"
            )

        if not cmd:
            raise ValueError(f"Model '{name}' has an empty command")

        # Extract explicit port from dict-style config
        explicit_port: int | None = None
        if isinstance(value, dict):
            explicit_port = value.get("port")
            if explicit_port is not None and not isinstance(explicit_port, int):
                raise ValueError(
                    f"Model '{name}' 'port' must be an integer, "
                    f"got {type(explicit_port).__name__}"
                )

        models[name] = ModelConfig(
            name=name, cmd=cmd, _explicit_port=explicit_port
        )

    if not models:
        raise ValueError("Config must define at least one model")

    _resolve_ports(models)
    return Config(models=models)
