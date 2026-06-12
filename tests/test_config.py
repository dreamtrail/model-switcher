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

from src.config import Config, ModelConfig, load_config


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
            assert 1 <= mc.port <= 65535

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
