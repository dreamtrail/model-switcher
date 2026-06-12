"""YAML configuration loader with automatic port assignment."""

from __future__ import annotations

import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml



@dataclass
class ModelConfig:
    """Configuration for a single model."""

    name: str
    cmd: str  # command (rewritten with auto-assigned port)
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
    """Auto-assign a free port for each model.

    Port rewriting (substituting ${PORT}, replacing port flags, handling
    script files) is deferred to the Swapper which manages temp file lifecycle.

    For each model:
    1. Find a free port
    2. If ${PORT} placeholder present, substitute it directly
    3. Store the resolved port
    """
    for name, mc in models.items():
        port = _find_free_port()
        mc.port = port
        # Fast path: ${PORT} placeholder — substitute directly
        if "${PORT}" in mc.cmd:
            mc.cmd = re.sub(r"\$\{PORT\}", str(port), mc.cmd)


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

        models[name] = ModelConfig(name=name, cmd=cmd)

    if not models:
        raise ValueError("Config must define at least one model")

    _resolve_ports(models)
    return Config(models=models)
