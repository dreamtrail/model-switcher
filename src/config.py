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


@dataclass
class ModelConfig:
    """Configuration for a single model."""

    name: str
    cmd: str  # command with ${PORT} substituted after load
    port: int = 0  # resolved port


@dataclass
class Config:
    """Parsed configuration."""

    models: dict[str, ModelConfig] = field(default_factory=dict)


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _resolve_ports(models: dict[str, ModelConfig]) -> None:
    """Replace ${PORT} in each model's command with a free port."""
    for name, mc in models.items():
        port = _find_free_port()
        mc.port = port
        mc.cmd = _PORT_RE.sub(str(port), mc.cmd)


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

        if _PORT_PLACEHOLDER not in cmd:
            raise ValueError(
                f"Model '{name}' command must contain '{_PORT_PLACEHOLDER}' "
                f"placeholder for automatic port assignment"
            )

        models[name] = ModelConfig(name=name, cmd=cmd)

    if not models:
        raise ValueError("Config must define at least one model")

    _resolve_ports(models)
    return Config(models=models)
