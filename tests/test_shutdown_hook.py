"""Local subprocess evidence for the shutdown-hook contract (no remote calls)."""

import asyncio
import json
import shlex
import sys
from unittest.mock import AsyncMock

import pytest
import yaml

from src.config import load_config
from src.process_manager import ProcessHandle, ProcessStopError
from src.shutdown_hook import run_shutdown_hook


def command(code, *args):
    return shlex.join([sys.executable, "-c", code, *map(str, args)])


def handle(cmd, timeout=2):
    return ProcessHandle("a", 1, None, shutdown_cmd=cmd, shutdown_timeout=timeout)


@pytest.mark.parametrize("value", [
    {"shutdown_cmd": ""}, {"shutdown_cmd": "  "},
    {"shutdown_cmd": "'"}, {"shutdown_cmd": "''"},
    {"shutdown_cmd": None}, {"shutdown_cmd": []},
    {"shutdown_cmd": "a\x00b"}, {"shutdown_timeout": 1},
    *[{"shutdown_cmd": "echo", "shutdown_timeout": t}
      for t in [True, False, 0, -1, 10**400, float("inf"), float("nan"), "30", None]],
])
def test_invalid_config(tmp_path, value):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"a": {"cmd": "echo", **value}}))
    with pytest.raises(ValueError):
        load_config(path)


@pytest.mark.parametrize("timeout", [0.25, 30, 120])
def test_positive_config_timeout(tmp_path, timeout):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"a": {
        "cmd": "echo", "shutdown_cmd": "echo", "shutdown_timeout": timeout,
    }}))
    assert load_config(path).models["a"].shutdown_timeout == timeout


def test_compatible_config_and_literal_hook(tmp_path):
    path = tmp_path / "config.yaml"
    literal = 'echo "${PORT}" --port 123'
    path.write_text(yaml.safe_dump({
        "old": "echo ${PORT}", "mapping": {"cmd": "echo"},
        "new": {"cmd": "echo ${PORT}", "shutdown_cmd": literal},
    }))
    config = load_config(path)
    assert config.models["old"].shutdown_cmd is None
    assert config.models["mapping"].shutdown_timeout == 30
    assert config.models["new"].shutdown_cmd == literal
    assert "${PORT}" not in config.models["new"].cmd


async def test_literal_arguments_environment_cwd_and_no_output(tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOOK_TEST_VALUE", "inherited")
    output = tmp_path / "observed.json"
    h = handle(command(
        "import sys,os,json; from pathlib import Path; "
        "Path(sys.argv[1]).write_text(json.dumps([sys.argv[2:],os.getcwd(),"
        "os.environ['HOOK_TEST_VALUE'],sys.stdin.read()])); "
        "print('PRIVATE_OUTPUT'); print('PRIVATE_ERROR',file=sys.stderr)",
        output, "${PORT}", "$HOME", "a b", ";", "--port", "123",
    ))
    await run_shutdown_hook(h)
    assert json.loads(output.read_text()) == [
        ["${PORT}", "$HOME", "a b", ";", "--port", "123"],
        str(tmp_path), "inherited", "",
    ]
    assert h.hook_succeeded
    assert "PRIVATE" not in caplog.text
    assert "HOOK_TEST_VALUE" not in caplog.text


@pytest.mark.parametrize("cmd", ["/missing/PRIVATE_EXEC", command("import sys; print('PRIVATE'); sys.exit(7)")])
async def test_failure_is_sanitized(cmd, caplog):
    h = handle(cmd)
    with pytest.raises(ProcessStopError, match="^Shutdown hook failed$") as error:
        await run_shutdown_hook(h)
    assert not h.hook_succeeded
    assert error.value.__suppress_context__
    assert "PRIVATE" not in caplog.text


async def test_timeout_kills_resistant_process(tmp_path, monkeypatch):
    import src.process_manager as pm
    monkeypatch.setattr(pm, "GRACE_PERIOD", 0.1)
    marker = tmp_path / "ready"
    h = handle(command(
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(marker)!r}).touch(); time.sleep(60)"
    ), timeout=0.3)
    with pytest.raises(ProcessStopError):
        await run_shutdown_hook(h)
    assert marker.exists()
    assert h.hook_process is None
    assert not h.hook_succeeded


async def test_unresolved_hook_group_blocks_new_attempt(monkeypatch):
    import src.shutdown_hook as hooks
    real_stop = hooks.stop_process
    failing_stop = AsyncMock(side_effect=ProcessStopError("private"))
    monkeypatch.setattr(hooks, "stop_process", failing_stop)
    h = handle(command("pass"))
    with pytest.raises(ProcessStopError):
        await run_shutdown_hook(h)
    pending = h.hook_process
    spawn = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(hooks.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ProcessStopError):
        await run_shutdown_hook(h)
    assert h.hook_process is pending
    spawn.assert_not_called()
    monkeypatch.setattr(hooks, "stop_process", real_stop)
    await run_shutdown_hook(h)
    assert h.hook_succeeded
    assert h.hook_process is None


async def test_cleans_resistant_child_after_hook_launcher_exit(tmp_path, monkeypatch):
    import os
    import signal
    import src.process_manager as pm
    from tests.test_process_manager import _wait_for_pid_gone

    monkeypatch.setattr(pm, "GRACE_PERIOD", 0.1)
    pid_file = tmp_path / "child.pid"
    ready = tmp_path / "child.ready"
    child_code = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(ready)!r}).touch(); time.sleep(60)"
    )
    h = handle(command(
        "import subprocess,sys,time; from pathlib import Path; "
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        f"Path({str(pid_file)!r}).write_text(str(p.pid)); "
        f"\nwhile not Path({str(ready)!r}).exists(): time.sleep(.01)"
    ))
    try:
        await run_shutdown_hook(h)
        assert h.hook_succeeded  # launcher exited zero; child group cleaned
        assert await _wait_for_pid_gone(int(pid_file.read_text()))
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
