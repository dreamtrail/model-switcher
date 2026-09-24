from __future__ import annotations

import builtins
import os
from pathlib import Path
import shutil

from src.swapper import Swapper
from tests.control_helpers import handle
from tests.quick_switch_helpers import make_config


class TestControlPersistenceBoundary:
    async def test_settings_load_and_unload_use_no_write_capable_filesystem_entrypoint(
        self, monkeypatch
    ):
        import src.swapper as module

        swapper = Swapper(make_config(), idle_timeout=0)
        config_snapshot = {
            name: (model.cmd, model.port)
            for name, model in swapper.config.models.items()
        }
        writes = []
        recording = True

        async def start(model, cmd, port):
            return handle(model)

        async def ready(*args, **kwargs):
            return None

        async def stop(tracked):
            tracked.process.returncode = 0

        monkeypatch.setattr(module, "start_process", start)
        monkeypatch.setattr(module, "wait_for_ready", ready)
        monkeypatch.setattr(module, "stop_process", stop)

        def note(name, *details):
            if recording:
                writes.append((name, details))

        original_open = builtins.open

        def audited_open(file, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                note("builtins.open", file, mode)
            return original_open(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", audited_open)

        original_path_open = Path.open

        def audited_path_open(path, mode="r", *args, **kwargs):
            if any(flag in mode for flag in "wax+"):
                note("Path.open", path, mode)
            return original_path_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", audited_path_open)

        original_os_open = os.open
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

        def audited_os_open(path, flags, *args, **kwargs):
            if flags & write_flags:
                note("os.open", path, flags)
            return original_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(os, "open", audited_os_open)

        def audit_all_calls(owner, names):
            for name in names:
                original = getattr(owner, name)

                def audited(*args, _name=name, _original=original, **kwargs):
                    note(f"{owner.__name__}.{_name}", *args[:2])
                    return _original(*args, **kwargs)

                monkeypatch.setattr(owner, name, audited)

        audit_all_calls(
            Path,
            (
                "write_text",
                "write_bytes",
                "touch",
                "mkdir",
                "unlink",
                "rename",
                "replace",
            ),
        )
        audit_all_calls(
            os,
            ("mkdir", "makedirs", "remove", "unlink", "rename", "replace"),
        )
        audit_all_calls(shutil, ("copyfile", "copy", "copy2", "move", "rmtree"))

        try:
            assert swapper.control_apply_settings(
                automatic_switch=False,
                quick_switch=True,
                idle_timeout_seconds=0,
            ) == "success"
            assert await swapper.control_load_model("a") == "success"
            assert await swapper.control_unload_model() == "success"

            assert writes == []
            assert {
                name: (model.cmd, model.port)
                for name, model in swapper.config.models.items()
            } == config_snapshot
            assert swapper.control_state()["lifecycle"] == "idle"
        finally:
            # Swapper shutdown legitimately removes its construction-time
            # temporary directory; it is not a control-triggered write.
            recording = False
            await swapper.shutdown()
