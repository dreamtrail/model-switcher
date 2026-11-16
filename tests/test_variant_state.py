"""Saved active variants across restarts (P-20261013-02).

Load/save rules of src/variant_state.py on real files, plus the Swapper
hooks (switch and reload) with faked processes. The real restart of the
switcher process lives in tests/test_variant_state_e2e.py.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from src.config import load_config
from src.variant_state import VariantStateStore, state_path
from tests.config_reload_helpers import cleanup, fakes, make_swapper, write


def _qwen(default: str = "text", names=("text", "vision")) -> dict:
    return {
        "plain": "echo plain --port ${PORT}",
        "qwen": {
            "default": default,
            "variants": {
                name: f"echo {name} --port ${{PORT}}" for name in names
            },
        },
    }


def _config(tmp_path: Path, **kwargs):
    path = tmp_path / "config.yaml"
    write(path, _qwen(**kwargs))
    return load_config(path)


def _state(tmp_path: Path, active: str, default: str = "text") -> Path:
    path = tmp_path / "config.state.json"
    path.write_text(json.dumps({
        "version": 1,
        "variants": {"qwen": {"active": active, "default": default}},
    }))
    return path


def _saved(path: Path) -> dict:
    return json.loads(path.read_text())


def test_state_file_sits_next_to_the_config():
    assert state_path("/srv/cfg/config.yaml") == Path(
        "/srv/cfg/config.state.json"
    )
    assert state_path("models.yml") == Path("models.state.json")


class TestRestore:
    def test_missing_file_uses_yaml_default_silently(self, tmp_path, caplog):
        config = _config(tmp_path)
        caplog.set_level(logging.INFO)
        VariantStateStore(tmp_path / "config.state.json").restore(config)
        assert config.variants["qwen"].active == "text"
        assert "Variant state" not in caplog.text
        assert not (tmp_path / "config.state.json").exists()

    def test_unchanged_default_restores_the_pick(self, tmp_path, caplog):
        config = _config(tmp_path)
        caplog.set_level(logging.INFO)
        VariantStateStore(_state(tmp_path, "vision")).restore(config)
        assert config.variants["qwen"].active == "vision"
        assert config.resolve("qwen") == "qwen@vision"
        assert "qwen restored to variant 'vision'" in caplog.text

    def test_changed_default_wins_and_is_logged(self, tmp_path, caplog):
        config = _config(tmp_path, default="vision",
                         names=("text", "vision", "big"))
        caplog.set_level(logging.INFO)
        VariantStateStore(_state(tmp_path, "big")).restore(config)
        assert config.variants["qwen"].active == "vision"
        assert (
            "qwen starts on default 'vision'; saved variant 'big' ignored "
            "because default changed from 'text'"
        ) in caplog.text

    def test_removed_variant_falls_back_with_warning(self, tmp_path, caplog):
        config = _config(tmp_path)
        caplog.set_level(logging.INFO)
        VariantStateStore(_state(tmp_path, "gone")).restore(config)
        assert config.variants["qwen"].active == "text"
        records = [r for r in caplog.records if "no longer exists" in
                   r.getMessage()]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING

    def test_saved_variant_that_became_default_is_not_ignored(
        self, tmp_path, caplog
    ):
        config = _config(tmp_path, default="vision")
        caplog.set_level(logging.INFO)
        VariantStateStore(_state(tmp_path, "vision")).restore(config)
        assert config.variants["qwen"].active == "vision"
        assert "ignored" not in caplog.text
        assert (
            "qwen starts on variant 'vision' (saved, and now also the "
            "default)"
        ) in caplog.text

    def test_unknown_model_in_file_is_ignored(self, tmp_path):
        config = _config(tmp_path)
        path = tmp_path / "config.state.json"
        path.write_text(json.dumps({"version": 1, "variants": {
            "other": {"active": "x", "default": "y"},
        }}))
        VariantStateStore(path).restore(config)
        assert config.variants["qwen"].active == "text"

    def test_corrupt_or_wrong_shape_file_warns_and_uses_defaults(
        self, tmp_path, caplog
    ):
        path = tmp_path / "config.state.json"
        bad = [
            "{not json",
            "[]",
            json.dumps({"version": 2, "variants": {}}),
            json.dumps({"version": 1, "variants": []}),
            json.dumps({"version": 1, "variants": {"qwen": "vision"}}),
            json.dumps({"version": 1, "variants": {
                "qwen": {"active": "vision"}}}),
        ]
        for text in bad:
            caplog.clear()
            path.write_text(text)
            config = _config(tmp_path)
            with caplog.at_level(logging.WARNING):
                VariantStateStore(path).restore(config)
            assert config.variants["qwen"].active == "text", text
            assert "is invalid" in caplog.text, text
            assert "using YAML defaults" in caplog.text, text

    def test_unreadable_file_warns_and_uses_defaults(self, tmp_path, caplog):
        path = _state(tmp_path, "vision")
        path.chmod(0)
        try:
            if os.access(path, os.R_OK):
                return  # permissions do not stop this user (root)
            config = _config(tmp_path)
            with caplog.at_level(logging.WARNING):
                VariantStateStore(path).restore(config)
            assert config.variants["qwen"].active == "text"
            assert "not readable" in caplog.text
        finally:
            path.chmod(0o600)


class TestSave:
    def test_save_writes_all_groups_atomically(self, tmp_path):
        config = _config(tmp_path)
        path = tmp_path / "config.state.json"
        store = VariantStateStore(path)
        store.restore(config)
        config.variants["qwen"].active = "vision"
        store.save(config, picked="qwen")
        assert _saved(path) == {"version": 1, "variants": {
            "qwen": {"active": "vision", "default": "text"},
        }}
        # No temp files left behind.
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "config.state.json", "config.yaml",
        ]

    def test_save_skips_when_nothing_changed(self, tmp_path):
        config = _config(tmp_path)
        path = _state(tmp_path, "vision")
        store = VariantStateStore(path)
        store.restore(config)
        os.utime(path, ns=(1, 1))
        store.save(config, picked="qwen")
        assert path.stat().st_mtime_ns == 1

    def test_defaults_alone_create_no_file(self, tmp_path):
        config = _config(tmp_path)
        store = VariantStateStore(tmp_path / "config.state.json")
        store.restore(config)
        store.save(config)
        assert not (tmp_path / "config.state.json").exists()
        # An explicit pick is saved even when it is the default.
        store.save(config, picked="qwen")
        assert _saved(tmp_path / "config.state.json")["variants"] == {
            "qwen": {"active": "text", "default": "text"},
        }

    def test_write_error_is_a_warning(self, tmp_path, caplog):
        config = _config(tmp_path)
        store = VariantStateStore(tmp_path / "missing-dir" / "s.state.json")
        config.variants["qwen"].active = "vision"
        with caplog.at_level(logging.WARNING):
            store.save(config, picked="qwen")
        assert "Could not save variant state to s.state.json" in caplog.text

    def test_pick_keeps_the_default_it_was_made_under(self, tmp_path):
        """A reload that keeps the active variant while the YAML default
        changed must not make the old pick look newer than the edit."""
        config = _config(tmp_path)
        path = _state(tmp_path, "vision")
        store = VariantStateStore(path)
        store.restore(config)
        reloaded = _config(tmp_path, default="big",
                           names=("text", "vision", "big"))
        reloaded.variants["qwen"].active = "vision"  # kept by reload
        store.save(reloaded)
        assert _saved(path)["variants"]["qwen"] == {
            "active": "vision", "default": "text",
        }
        # Next start: the YAML edit wins.
        fresh = _config(tmp_path, default="big",
                        names=("text", "vision", "big"))
        VariantStateStore(path).restore(fresh)
        assert fresh.variants["qwen"].active == "big"

    def test_reload_kept_default_never_looks_like_a_pick(self, tmp_path):
        """S1: never picked, ``default:`` edited and reloaded (old default
        kept active), then restart: the new YAML default wins."""
        config = _config(tmp_path)
        path = tmp_path / "config.state.json"
        store = VariantStateStore(path)
        store.restore(config)
        reloaded = _config(tmp_path, default="vision")
        reloaded.variants["qwen"].active = "text"  # kept by reload
        store.save(reloaded)
        assert not path.exists()  # nothing was picked
        fresh = _config(tmp_path, default="vision")
        VariantStateStore(path).restore(fresh)
        assert fresh.variants["qwen"].active == "vision"

    def test_reload_kept_default_keeps_its_start_default_in_file(
        self, tmp_path
    ):
        """Same as above with a file already on disk (another model was
        picked): the kept variant records the default it started under."""
        path = tmp_path / "config.yaml"
        write(path, {**_qwen(), "gemma": {
            "variants": {"a": "echo a --port ${PORT}",
                         "b": "echo b --port ${PORT}"},
        }})
        config = load_config(path)
        store = VariantStateStore(state_path(path))
        store.restore(config)
        config.variants["gemma"].active = "b"
        store.save(config, picked="gemma")

        write(path, {**_qwen(default="vision"), "gemma": {
            "variants": {"a": "echo a --port ${PORT}",
                         "b": "echo b --port ${PORT}"},
        }})
        reloaded = load_config(path)
        reloaded.variants["qwen"].active = "text"  # kept by reload
        reloaded.variants["gemma"].active = "b"
        store.save(reloaded)
        assert _saved(state_path(path))["variants"] == {
            "qwen": {"active": "text", "default": "text"},
            "gemma": {"active": "b", "default": "a"},
        }
        fresh = load_config(path)
        VariantStateStore(state_path(path)).restore(fresh)
        assert fresh.variants["qwen"].active == "vision"
        assert fresh.variants["gemma"].active == "b"

    def test_explicit_pick_records_the_current_default(self, tmp_path):
        config = _config(tmp_path, default="big",
                         names=("text", "vision", "big"))
        path = _state(tmp_path, "vision")  # saved under default "text"
        store = VariantStateStore(path)
        store.restore(config)  # ignored: default changed
        config.variants["qwen"].active = "vision"
        store.save(config, picked="qwen")
        assert _saved(path)["variants"]["qwen"] == {
            "active": "vision", "default": "big",
        }


class TestSwapperHooks:
    async def test_switch_saves_and_restart_restores(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.variant_state = VariantStateStore(state_path(path))
        swapper.variant_state.restore(swapper.config)
        assert await swapper.control_load_model("qwen", "vision") == "success"
        assert _saved(state_path(path))["variants"]["qwen"] == {
            "active": "vision", "default": "text",
        }
        config = load_config(path)
        VariantStateStore(state_path(path)).restore(config)
        assert config.resolve("qwen") == "qwen@vision"

    async def test_unwritable_state_never_blocks_a_switch(
        self, tmp_path, fakes, cleanup, caplog
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.variant_state = VariantStateStore(
            tmp_path / "no-such-dir" / "config.state.json"
        )
        with caplog.at_level(logging.WARNING):
            kind = await swapper.control_load_model("qwen", "vision")
        assert kind == "success"
        assert swapper.config.variants["qwen"].active == "vision"
        assert swapper.active_model == "qwen@vision"
        assert "Could not save variant state" in caplog.text

    async def test_save_crash_never_blocks_a_switch(
        self, tmp_path, fakes, cleanup, caplog
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)

        class Broken:
            def save(self, config, picked=None):
                raise RuntimeError("boom")

        swapper.variant_state = Broken()
        assert await swapper.control_load_model("qwen", "vision") == "success"
        assert "Could not save variant state" in caplog.text

    async def test_reload_saves_when_active_variant_was_removed(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.variant_state = VariantStateStore(state_path(path))
        swapper.variant_state.restore(swapper.config)
        assert await swapper.control_load_model("qwen", "vision") == "success"
        await swapper.control_unload_model()

        write(path, _qwen(names=("text", "big")))
        kind, _ = swapper.control_reload_config()
        assert kind == "success"
        assert swapper.config.variants["qwen"].active == "text"
        assert _saved(state_path(path))["variants"]["qwen"] == {
            "active": "text", "default": "text",
        }

    async def test_default_edit_reload_then_restart_uses_new_default(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.variant_state = VariantStateStore(state_path(path))
        swapper.variant_state.restore(swapper.config)

        write(path, _qwen(default="vision"))
        kind, _ = swapper.control_reload_config()
        assert kind == "success"
        assert swapper.config.variants["qwen"].active == "text"  # kept
        assert not state_path(path).exists()

        # Restart: the YAML edit wins.
        config = load_config(path)
        VariantStateStore(state_path(path)).restore(config)
        assert config.resolve("qwen") == "qwen@vision"

    async def test_reload_without_variant_change_does_not_write(
        self, tmp_path, fakes, cleanup
    ):
        path = tmp_path / "config.yaml"
        write(path, _qwen())
        swapper = make_swapper(path)
        cleanup.append(swapper)
        swapper.variant_state = VariantStateStore(state_path(path))
        swapper.variant_state.restore(swapper.config)
        assert await swapper.control_load_model("qwen", "vision") == "success"
        os.utime(state_path(path), ns=(1, 1))

        write(path, {**_qwen(), "extra": "echo extra --port ${PORT}"})
        kind, _ = swapper.control_reload_config()
        assert kind == "success"
        assert state_path(path).stat().st_mtime_ns == 1
