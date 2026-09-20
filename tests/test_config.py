"""Tests for src/config.py — YAML config loading and port assignment.

Derived from plan P-20260612-07 acceptance criteria:
- Config parsing: valid YAML, ${PORT} substitution
- Auto-assign free port for each model
- Port rewriting deferred to Swapper (temp file lifecycle)
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

import src.config as config_module
from src.config import Config, ModelConfig, load_config


class _ProbeSocket:
    """Deterministic socket probe used to observe pair lifetime and retries."""

    def __init__(self, factory):
        self.factory = factory
        self.candidate = None
        self.closed = False
        factory.sockets.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()

    def bind(self, address):
        host, port = address
        self.factory.binds.append(address)
        if port == 0:
            self.candidate = self.factory.candidates.pop(0)
            self.factory.active_base = self
            return

        self.factory.simultaneous_checks.append(
            self.factory.active_base is not None
            and not self.factory.active_base.closed
        )
        if port in self.factory.occupied:
            raise OSError("address already in use")

    def getsockname(self):
        return ("127.0.0.1", self.candidate)

    def close(self):
        self.closed = True


class _ProbeSocketFactory:
    def __init__(self, candidates, *, occupied=()):
        self.candidates = list(candidates)
        self.occupied = set(occupied)
        self.sockets = []
        self.binds = []
        self.simultaneous_checks = []
        self.active_base = None

    def __call__(self, *args, **kwargs):
        return _ProbeSocket(self)


class TestFindFreePort:

    def test_retries_occupied_adjacent_port_and_closes_simultaneous_probes(
        self, monkeypatch
    ):
        factory = _ProbeSocketFactory([41000, 42000], occupied={41001})
        monkeypatch.setattr(config_module.socket, "socket", factory)

        port = config_module._find_free_port()

        assert port == 42000
        assert 1 <= port <= 65534
        assert factory.binds == [
            ("127.0.0.1", 0),
            ("127.0.0.1", 41001),
            ("127.0.0.1", 0),
            ("127.0.0.1", 42001),
        ]
        assert factory.simultaneous_checks == [True, True]
        assert all(probe.closed for probe in factory.sockets)

    def test_retries_base_65535_without_probing_out_of_range_adjacent(
        self, monkeypatch
    ):
        factory = _ProbeSocketFactory([65535, 43000])
        monkeypatch.setattr(config_module.socket, "socket", factory)

        port = config_module._find_free_port()

        assert port == 43000
        assert ("127.0.0.1", 65536) not in factory.binds
        assert factory.binds == [
            ("127.0.0.1", 0),
            ("127.0.0.1", 0),
            ("127.0.0.1", 43001),
        ]
        assert factory.simultaneous_checks == [True]
        assert all(probe.closed for probe in factory.sockets)


class TestLoadConfig:

    def test_loads_valid_string_style(self, tmp_config):
        """Valid YAML with string commands loads successfully."""
        path = tmp_config({
            "model-a": "echo hello --port ${PORT}",
            "model-b": "echo world --port ${PORT}",
        })
        config = load_config(path)
        assert isinstance(config, Config)
        assert len(config.models) == 2
        assert "model-a" in config.models
        assert "model-b" in config.models

    def test_loads_dict_style(self, dict_config: Config):
        """Dict-style config with 'cmd' key loads successfully."""
        assert "model-a" in dict_config.models
        assert dict_config.models["model-a"].cmd is not None

    def test_port_substituted_in_command(self, simple_config: Config):
        """${PORT} is replaced with an actual port number in the command."""
        for name, mc in simple_config.models.items():
            assert "${PORT}" not in mc.cmd
            assert mc.port > 0

    def test_ports_are_unique(self, simple_config: Config):
        """Each model gets a different port."""
        ports = [mc.port for mc in simple_config.models.values()]
        assert len(ports) == len(set(ports))

    def test_ports_are_valid_range(self, simple_config: Config):
        """Assigned ports are in valid range."""
        for mc in simple_config.models.values():
            assert 1 <= mc.port <= 65534

    def test_natural_command_port_assigned(self, tmp_config):
        """Commands without ${PORT} still get a port assigned."""
        path = tmp_config({
            "model-a": "echo hello --port 8000",
        })
        config = load_config(path)
        assert config.models["model-a"].port > 0
        # Port rewriting (replacing 8000 with free port) is deferred to Swapper

    def test_script_command_port_assigned(self, tmp_config):
        """Script commands get a port assigned (rewriting deferred to Swapper)."""
        path = tmp_config({
            "model-a": "./start-model.sh",
        })
        config = load_config(path)
        assert config.models["model-a"].port > 0

    def test_empty_command_rejected(self, tmp_config):
        """Model with empty command raises ValueError."""
        path = tmp_config({
            "model-a": "",
        })
        with pytest.raises(ValueError, match="empty command"):
            load_config(path)

    def test_no_models_rejected(self, tmp_config):
        """Config with no models raises ValueError."""
        path = tmp_config({})
        with pytest.raises(ValueError, match="at least one model"):
            load_config(path)

    def test_invalid_model_value_type_rejected(self, tmp_config):
        """Model value that is not string or dict raises ValueError."""
        path = tmp_config({
            "model-a": 12345,
        })
        with pytest.raises(ValueError, match="string or mapping"):
            load_config(path)

    def test_dict_without_cmd_key_rejected(self, tmp_config):
        """Dict-style model without 'cmd' key raises ValueError for empty cmd."""
        path = tmp_config({
            "model-a": {"other": "value"},
        })
        with pytest.raises(ValueError, match="empty command"):
            load_config(path)

    def test_dict_style_loads(self, tmp_config):
        """Dict-style model with cmd key loads successfully."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello --port ${PORT}"},
        })
        config = load_config(path)
        assert config.models["model-a"].port > 0
        assert "${PORT}" not in config.models["model-a"].cmd

    def test_non_dict_yaml_rejected(self, tmp_path):
        """YAML that is a list (not a mapping) raises ValueError."""
        path = tmp_path / "config.yaml"
        path.write_text(yaml.dump(["model-a", "model-b"]))
        with pytest.raises(ValueError, match="YAML mapping"):
            load_config(path)

    def test_missing_file_raises_file_not_found(self, tmp_path):
        """Nonexistent config file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_model_name_preserved(self, simple_config: Config):
        """Model name in ModelConfig matches the config key."""
        for name, mc in simple_config.models.items():
            assert mc.name == name
