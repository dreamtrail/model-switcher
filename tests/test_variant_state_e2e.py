"""Active variant survives a real switcher restart (P-20261013-02).

Starts ``python -m src`` with a temp variant config, picks a variant through
the control API, stops the switcher, starts it again, and checks the
control state, the state file, and which model server the next request
launches. Then a YAML default change wins over the saved pick. Nothing is
faked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import urllib.request

import pytest
import yaml

from src.control import CONTROL_HEADER
from tests.test_cli_sighup_e2e import (
    REPO_ROOT,
    _free_port,
    _kill_model_servers,
    _wait_for,
)
from tests.test_variants_e2e import SERVER


def _request(url: str, *, method: str = "GET", data: dict | None = None):
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json", CONTROL_HEADER: "1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


class _Switcher:
    """One ``python -m src`` process; stop() sends SIGTERM and waits."""

    def __init__(self, config: Path, log: Path):
        self.config = config
        self.log = log
        self.proc: subprocess.Popen | None = None
        self.base = ""

    def start(self) -> None:
        env = {k: v for k, v in os.environ.items()
               if k != "PYTHONUNBUFFERED"}
        # A free port can be taken before the switcher binds it; retry.
        for attempt in range(3):
            port = _free_port()
            self.base = f"http://127.0.0.1:{port}"
            with self.log.open("a") as out:
                self.proc = subprocess.Popen(
                    [sys.executable, "-m", "src", "--config",
                     str(self.config), "--port", str(port),
                     "--idle-timeout", "0"],
                    cwd=REPO_ROOT, env=env, stdout=out,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            try:
                _wait_for(
                    lambda: self.proc.poll() is not None
                    or self.state()["lifecycle"] == "idle",
                    "switcher start",
                )
            except AssertionError:
                pytest.fail(f"switcher log:\n{self.log.read_text()}")
            if self.proc.poll() is None:
                return
            if "in use" not in self.log.read_text() or attempt == 2:
                pytest.fail(f"switcher exited:\n{self.log.read_text()}")

    def state(self) -> dict:
        return _request(f"{self.base}/api/v1/control/state")

    def stop(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            assert self.proc.wait(timeout=20) == 0
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)
            raise


def test_restart_starts_on_the_last_picked_variant(tmp_path):
    events = tmp_path / "events"
    pids = tmp_path / "pids"
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER)
    script = tmp_path / "run-model.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ >> {shlex.quote(str(pids))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server_py))} "
        f"--port 8000 --events {shlex.quote(str(events))} \"$@\"\n"
    )
    script.chmod(0o755)
    config_path = tmp_path / "config.yaml"
    state_file = tmp_path / "config.state.json"

    def write(default: str) -> None:
        config_path.write_text(yaml.safe_dump({"qwen": {
            "default": default,
            "variants": {
                name: f"{script} --name {name}"
                for name in ("text", "vision", "big")
            },
        }}, sort_keys=False))

    def chat(switcher: _Switcher) -> dict:
        return _request(
            f"{switcher.base}/v1/chat/completions", method="POST",
            data={"model": "qwen", "messages": []},
        )

    write("text")
    log_path = tmp_path / "switcher.log"
    switcher = _Switcher(config_path, log_path)
    try:
        # First run: no state file; pick "vision" on the control API.
        switcher.start()
        assert switcher.state()["variants"]["qwen"]["active"] == "text"
        assert not state_file.exists()
        state = _request(
            f"{switcher.base}/api/v1/control/model", method="PUT",
            data={"model": "qwen", "variant": "vision"},
        )
        assert state["active_model"] == "qwen@vision"
        assert json.loads(state_file.read_text()) == {
            "version": 1,
            "variants": {"qwen": {"active": "vision", "default": "text"}},
        }
        switcher.stop()

        # Second run: starts on "vision" and launches it for a request.
        switcher.start()
        assert switcher.state()["variants"]["qwen"]["active"] == "vision"
        assert chat(switcher)["served_by"] == "vision"
        switcher.stop()
        assert "qwen restored to variant 'vision'" in log_path.read_text()

        # Third run: the YAML default changed, so the YAML wins.
        write("big")
        switcher.start()
        assert switcher.state()["variants"]["qwen"]["active"] == "big"
        assert chat(switcher)["served_by"] == "big"
        switcher.stop()
        assert (
            "saved variant 'vision' ignored because default changed "
            "from 'text'"
        ) in log_path.read_text()

        # Fourth run: a corrupt state file never stops the switcher.
        state_file.write_text("{broken")
        switcher.start()
        assert switcher.state()["variants"]["qwen"]["active"] == "big"
        switcher.stop()
        assert "is invalid" in log_path.read_text()
    finally:
        try:
            switcher.stop()
        finally:
            _kill_model_servers(pids, server_py)

    launches = [
        line for line in events.read_text().splitlines()
        if line.startswith("launch ")
    ]
    assert launches == ["launch vision", "launch vision", "launch big"]
