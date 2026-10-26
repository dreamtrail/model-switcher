"""Shared fake-process harness and fixtures for config-reload tests."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import yaml

from src.config import load_config
from src.process_manager import ProcessHandle
from src.swapper import Swapper
from tests.quick_switch_helpers import FakeProcess


def write(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False))


class Fakes:
    """Fake process start/ready/stop; records what was started."""

    def __init__(self, monkeypatch):
        import src.swapper as module

        self.started: list[tuple[str, str, int]] = []
        self.stopped: list[str] = []
        self.ready_gate: asyncio.Event | None = None
        self.stop_gate: asyncio.Event | None = None
        self.on_stop = None

        async def start(model, cmd, port):
            self.started.append((model, cmd, port))
            return ProcessHandle(model, port, FakeProcess(len(self.started)))

        async def ready(*args, **kwargs):
            if self.ready_gate is not None:
                await self.ready_gate.wait()

        async def stop(handle):
            if self.on_stop is not None:
                self.on_stop()
            if self.stop_gate is not None:
                await self.stop_gate.wait()
            self.stopped.append(handle.model_name)
            handle.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)


def make_swapper(path: Path) -> Swapper:
    swapper = Swapper(load_config(path), idle_timeout=0)
    swapper.config_path = path
    return swapper


def unreadable_script(path: Path) -> Path:
    """A text script the switcher cannot read (the copy step must fail)."""
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(0)
    if os.access(path, os.R_OK):
        pytest.skip("file permissions do not stop this user from reading")
    return path


@pytest.fixture
def fakes(monkeypatch):
    return Fakes(monkeypatch)


@pytest.fixture
async def cleanup():
    swappers: list[Swapper] = []
    yield swappers
    for swapper in swappers:
        await swapper.shutdown()
