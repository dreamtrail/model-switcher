"""Shared fixtures for model-switcher tests."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import AsyncGenerator

import aiohttp
import pytest
import yaml

from src.config import Config, ModelConfig


@pytest.fixture
def tmp_config(tmp_path: Path):
    """Write a YAML config file and return its path."""
    def _write(data: dict) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(data))
        return path
    return _write


@pytest.fixture
def simple_config(tmp_config):
    """Return a Config with two models (string-style commands)."""
    path = tmp_config({
        "model-a": "python -m http.server ${PORT}",
        "model-b": "python -m http.server ${PORT}",
    })
    from src.config import load_config
    return load_config(path)


@pytest.fixture
def dict_config(tmp_config):
    """Return a Config with dict-style model entries."""
    path = tmp_config({
        "model-a": {"cmd": "python -m http.server ${PORT}"},
    })
    from src.config import load_config
    return load_config(path)


@pytest.fixture
def pytester_config():
    """Return pytest.ini content for asyncio mode."""
    return {"asyncio_mode": "auto"}
