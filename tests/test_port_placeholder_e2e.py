"""Real-process check that ${PORT} reaches the model once (P-20261013-09).

A small HTTP server records its argv and binds the port it was given. The
run goes through the real config loader, Swapper startup rewrite, process
start, health check and config reload; the test reads the argv the
process actually got.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

import yaml

from src.config import load_config
from src.swapper import Swapper


# Takes the port from the first token that is a number or --port=N, so it
# accepts positional, --port N, -p N and --port=N forms.
SERVER = r'''
import json, os, signal, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

argv = sys.argv[1:]
out_dir, name = argv[0], argv[1]
port = None
for token in argv[2:]:
    value = token.split("=", 1)[1] if token.startswith("--port=") else token
    if value.isdigit():
        port = int(value)
        break
with open(os.path.join(out_dir, name + ".json"), "w") as out:
    json.dump(argv[2:], out)

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

FORMS = {
    "positional": "${PORT}",
    "long": "--port ${PORT}",
    "short": "-p ${PORT}",
    "equals": "--port=${PORT}",
}


def _config(tmp_path: Path, tag: str) -> dict:
    server = tmp_path / "server.py"
    out = tmp_path / "argv"
    base = shlex.join([sys.executable, str(server), str(out)])
    data = {
        name: f"{base} {name} {form} --tag {tag}"
        for name, form in FORMS.items()
    }
    script = tmp_path / "start.sh"
    data["script"] = f"{script} script ${{PORT}} --tag {tag}"
    return data


def _setup(tmp_path: Path) -> Path:
    (tmp_path / "server.py").write_text(SERVER)
    (tmp_path / "argv").mkdir()
    script = tmp_path / "start.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(sys.executable)} "
        f"{shlex.quote(str(tmp_path / 'server.py'))} "
        f"{shlex.quote(str(tmp_path / 'argv'))} \"$@\"\n"
    )
    script.chmod(0o755)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(_config(tmp_path, "v1")))
    return path


def _port_count(args: list[str], port: int) -> int:
    return sum(
        1 for token in args
        if token in (str(port), f"--port={port}")
    )


async def _check_all(swapper: Swapper, tmp_path: Path, tag: str) -> None:
    for name in [*FORMS, "script"]:
        record = tmp_path / "argv" / f"{name}.json"
        record.unlink(missing_ok=True)
        assert await swapper.control_load_model(name) == "success", name
        port = swapper.config.models[name].port
        args = json.loads(record.read_text())
        assert _port_count(args, port) == 1, (name, args)
        assert "--tag" in args and tag in args, (name, args)


async def test_port_placeholder_once_at_startup_and_reload(tmp_path):
    path = _setup(tmp_path)
    swapper = Swapper(load_config(path), timeout=30, idle_timeout=0)
    swapper.config_path = path
    try:
        await _check_all(swapper, tmp_path, "v1")

        # Change every entry so each gets a fresh port on reload.
        path.write_text(yaml.safe_dump(_config(tmp_path, "v2")))
        kind, detail = swapper.control_reload_config()
        assert kind == "success", detail
        assert sorted(detail["changed"]) == sorted([*FORMS, "script"])
        await _check_all(swapper, tmp_path, "v2")
    finally:
        await swapper.shutdown()
