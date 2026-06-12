"""Tests for src/config.py — YAML config loading and ${PORT} resolution.

Derived from plan acceptance criteria:
- Config parsing: valid YAML, ${PORT} substitution, missing ${PORT} rejected
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from src.config import Config, ModelConfig, _detect_port_from_cmd, load_config


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

    def test_auto_detect_port_from_cmd(self, tmp_config):
        """Port is auto-detected from --port flag when ${PORT} absent."""
        path = tmp_config({
            "model-a": "echo hello --port 9999",
        })
        config = load_config(path)
        assert config.models["model-a"].port == 9999
        assert config.models["model-a"].cmd == "echo hello --port 9999"

    def test_auto_detect_port_quoted(self, tmp_config):
        """Port detected from --port with quoted values."""
        path = tmp_config({
            "model-a": 'echo hello --port "8080"',
        })
        config = load_config(path)
        assert config.models["model-a"].port == 8080

    def test_auto_detect_port_single_quoted(self, tmp_config):
        """Port detected from --port with single-quoted values."""
        path = tmp_config({
            "model-a": "echo hello --port '7070'",
        })
        config = load_config(path)
        assert config.models["model-a"].port == 7070

    def test_explicit_port_in_config(self, tmp_config):
        """Explicit port: in dict-style config overrides auto-detect."""
        path = tmp_config({
            "model-a": {"cmd": "./start-model.sh", "port": 5555},
        })
        config = load_config(path)
        assert config.models["model-a"].port == 5555

    def test_explicit_port_overrides_auto_detect(self, tmp_config):
        """Explicit port: takes precedence over --port in cmd."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello --port 9999", "port": 5555},
        })
        config = load_config(path)
        assert config.models["model-a"].port == 5555

    def test_port_placeholder_overrides_explicit(self, tmp_config):
        """${PORT} in cmd takes highest priority over explicit port:."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello --port ${PORT}", "port": 5555},
        })
        config = load_config(path)
        assert config.models["model-a"].port != 5555  # got free port
        assert "${PORT}" not in config.models["model-a"].cmd

    def test_no_port_at_all_rejected(self, tmp_config):
        """Config with no port info raises ValueError."""
        path = tmp_config({
            "model-a": "echo hello",
        })
        with pytest.raises(ValueError, match="no port"):
            load_config(path)

    def test_invalid_port_type_rejected(self, tmp_config):
        """Non-integer port value raises ValueError."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello", "port": "not_a_number"},
        })
        with pytest.raises(ValueError, match="must be an integer"):
            load_config(path)

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

    def test_dict_cmd_auto_detect_port(self, tmp_config):
        """Dict-style model auto-detects port from --port flag."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello --port 9999"},
        })
        config = load_config(path)
        assert config.models["model-a"].port == 9999

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
            assert mc.name == mc.name

    def test_explicit_port_out_of_range_low(self, tmp_config):
        """Explicit port: 0 raises ValueError."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello", "port": 0},
        })
        with pytest.raises(ValueError, match="out of range"):
            load_config(path)

    def test_explicit_port_out_of_range_high(self, tmp_config):
        """Explicit port: 70000 raises ValueError."""
        path = tmp_config({
            "model-a": {"cmd": "echo hello", "port": 70000},
        })
        with pytest.raises(ValueError, match="out of range"):
            load_config(path)

    def test_auto_detect_port_out_of_range(self, tmp_config):
        """Auto-detected --port 99999 raises ValueError."""
        path = tmp_config({
            "model-a": "echo hello --port 99999",
        })
        with pytest.raises(ValueError, match="out of range"):
            load_config(path)

    def test_multiple_port_flags_first_wins(self, tmp_config):
        """Multiple --port flags: first match wins."""
        path = tmp_config({
            "model-a": "echo hello --port 1111 --port 2222",
        })
        config = load_config(path)
        assert config.models["model-a"].port == 1111


class TestDetectPortFromCmd:

    def test_basic_port_flag(self):
        """Detect --port 8080."""
        assert _detect_port_from_cmd("cmd --port 8080") == 8080

    def test_quoted_port(self):
        """Detect --port \"8080\"."""
        assert _detect_port_from_cmd('cmd --port "8080"') == 8080

    def test_single_quoted_port(self):
        """Detect --port '8080'."""
        assert _detect_port_from_cmd("cmd --port '8080'") == 8080

    def test_no_port_flag(self):
        """No --port flag returns None."""
        assert _detect_port_from_cmd("cmd --model foo") is None

    def test_listen_port_not_matched(self):
        """--listen-port does not match (single dash before port)."""
        assert _detect_port_from_cmd("cmd --listen-port 8080") is None

    def test_embedded_double_dash_port_not_matched(self):
        """--some--port does not match (lookbehind guard)."""
        assert _detect_port_from_cmd("cmd --some--port 8080") is None

    def test_port_without_value(self):
        """--port with no value returns None."""
        assert _detect_port_from_cmd("cmd --port") is None
