"""SIGHUP reload in a real switcher process (P-20261013-05).

Starts ``python -m src`` with a temp config and a small real HTTP server
as the model, edits the config, sends SIGHUP, and checks that the new model
shows up in /v1/models while the process keeps running. Nothing is faked.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest
import yaml

from tests.test_variants_e2e import SERVER

REPO_ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGHUP"), reason="SIGHUP is not available"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str, *, data: dict | None = None) -> dict:
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _wait_for(predicate, what: str, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = predicate()
        except (urllib.error.URLError, ConnectionError, OSError):
            result = None
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what} ({result!r})")


def _model_ids(base: str) -> list[str]:
    return [item["id"] for item in _get_json(f"{base}/v1/models")["data"]]


def _kill_model_servers(pids: Path, server_py: Path) -> None:
    for line in pids.read_text().split() if pids.exists() else []:
        try:
            cmdline = Path(f"/proc/{int(line)}/cmdline").read_bytes()
            if str(server_py).encode() in cmdline:
                os.kill(int(line), signal.SIGKILL)
        except (OSError, ValueError):
            pass


def test_sighup_reloads_config_and_keeps_the_switcher_running(tmp_path):
    events = tmp_path / "events"
    pids = tmp_path / "pids"
    server_py = tmp_path / "server.py"
    server_py.write_text(SERVER)
    config_path = tmp_path / "config.yaml"

    # A shell script (the switcher copies it and sets the port), like the
    # other real-process tests.
    script = tmp_path / "run-model.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ >> {shlex.quote(str(pids))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(server_py))} "
        f"--port 8000 --events {shlex.quote(str(events))} \"$@\"\n"
    )
    script.chmod(0o755)

    def model_cmd(name: str) -> str:
        return f"{script} --name {name}"

    def write(names: list[str]) -> None:
        config_path.write_text(yaml.safe_dump(
            {name: model_cmd(name) for name in names}, sort_keys=False
        ))

    write(["alpha"])
    log_path = tmp_path / "switcher.log"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    proc = None
    try:
        # A free port can be taken between picking it and the switcher
        # binding it; retry a few times when that is why the start failed.
        for attempt in range(3):
            port = _free_port()
            base = f"http://127.0.0.1:{port}"
            with log_path.open("w") as log:
                proc = subprocess.Popen(
                    [sys.executable, "-m", "src", "--config",
                     str(config_path), "--port", str(port),
                     "--idle-timeout", "0"],
                    cwd=REPO_ROOT, env=env, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            try:
                _wait_for(
                    lambda: _model_ids(base) == ["alpha"]
                    or proc.poll() is not None,
                    "switcher start",
                )
            except AssertionError:
                pytest.fail(f"switcher log:\n{log_path.read_text()}")
            if proc.poll() is None:
                break
            if "in use" not in log_path.read_text() or attempt == 2:
                pytest.fail(f"switcher exited:\n{log_path.read_text()}")
        assert _model_ids(base) == ["alpha"]

        # Valid edit: the new model appears and the process keeps running.
        write(["alpha", "beta"])
        proc.send_signal(signal.SIGHUP)
        _wait_for(
            lambda: _model_ids(base) == ["alpha", "beta"], "reload via SIGHUP"
        )
        assert proc.poll() is None
        served = _get_json(
            f"{base}/v1/chat/completions",
            data={"model": "beta", "messages": []},
        )
        assert served["served_by"] == "beta"

        # Invalid edit: the old config stays and the process still runs.
        config_path.write_text("alpha: [unclosed\n")
        proc.send_signal(signal.SIGHUP)
        _wait_for(
            lambda: "SIGHUP: config not reloaded" in log_path.read_text(),
            "warning for the invalid config",
        )
        assert proc.poll() is None
        assert _model_ids(base) == ["alpha", "beta"]

        # A fixed config works again with another SIGHUP.
        write(["beta", "gamma"])
        proc.send_signal(signal.SIGHUP)
        _wait_for(
            lambda: _model_ids(base) == ["beta", "gamma"], "second reload"
        )
        assert proc.poll() is None

        # SIGTERM still stops it.
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0
        assert "SIGHUP: config reloaded" in log_path.read_text()
    finally:
        # Normal path: the process already exited. On a failure, stop it the
        # graceful way first so it also stops its running model server.
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
        # Model servers run in their own process group, so a killed switcher
        # can leave one behind. The script records each server's pid; kill
        # the ones still alive (checked by command line, so a reused pid is
        # left alone).
        _kill_model_servers(pids, server_py)
