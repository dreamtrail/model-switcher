"""Tests for src/config.py — YAML config loading and port assignment.

Derived from plan P-20260612-07 acceptance criteria:
- Config parsing: valid YAML, ${PORT} substitution
- Auto-assign free port for each model
- Port rewriting deferred to Swapper (temp file lifecycle)
"""

from __future__ import annotations

import datetime
import pathlib

import pytest
import yaml

import src.config as config_module
from src.config import Config, ModelConfig, load_config


class _ProbeSocket:
    """Deterministic probe for exact port, bind and closure checks."""

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
        if port in self.factory.occupied:
            raise OSError("address already in use")
        if port == 0:
            self.candidate = self.factory.candidates.pop(0)

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
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _ProbeSocket(self)


class TestFindFreePort:

    @pytest.mark.parametrize("candidate", (41000, 65535))
    def test_probes_only_base_port_and_closes_socket(self, monkeypatch, candidate):
        # Exact ephemeral ports (especially 65535) cannot be forced on the OS.
        factory = _ProbeSocketFactory([candidate], occupied={41001})
        monkeypatch.setattr(config_module.socket, "socket", factory)

        port = config_module._find_free_port()

        assert port == candidate
        assert 1 <= port <= 65535
        assert factory.calls == [
            ((config_module.socket.AF_INET, config_module.socket.SOCK_STREAM), {}),
        ]
        assert factory.binds == [("127.0.0.1", 0)]
        assert len(factory.sockets) == 1
        assert factory.sockets[0].closed


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


# ---------------------------------------------------------------------------
# P-20261013-01: model variants under one public name
# ---------------------------------------------------------------------------


class TestVariantConfig:
    def test_variants_expand_to_ids_with_own_cmd_port_and_hook(self, tmp_config):
        config = load_config(tmp_config({
            "plain": "srv --port ${PORT}",
            "qwen": {
                "default": "vision",
                "variants": {
                    "text": "text-srv --port ${PORT} -a qwen",
                    "vision": {
                        "cmd": "vision-srv --port ${PORT}",
                        "shutdown_cmd": "/bin/stop-vision",
                        "shutdown_timeout": 12,
                    },
                },
            },
        }))
        assert list(config.models) == ["plain", "qwen@text", "qwen@vision"]
        text, vision = config.models["qwen@text"], config.models["qwen@vision"]
        assert text.name == "qwen@text"
        assert text.cmd == f"text-srv --port {text.port} -a qwen"
        assert text.shutdown_cmd is None and text.shutdown_timeout == 30
        assert vision.cmd == f"vision-srv --port {vision.port}"
        assert vision.shutdown_cmd == "/bin/stop-vision"
        assert vision.shutdown_timeout == 12
        assert text.port != vision.port
        group = config.variants["qwen"]
        assert (group.variants, group.default, group.active) == (
            ["text", "vision"], "vision", "vision"
        )
        assert config.public_names() == ["plain", "qwen"]
        assert config.resolve("qwen") == "qwen@vision"
        assert config.resolve("plain") == "plain"
        # Internal ids are not public names.
        assert config.resolve("qwen@text") is None
        assert config.resolve("missing") is None

    def test_default_falls_back_to_first_variant(self, tmp_path):
        # Literal YAML: file order decides the first variant.
        path = tmp_path / "config.yaml"
        path.write_text(
            "m:\n"
            "  variants:\n"
            "    b: \"x --port ${PORT}\"\n"
            "    a: \"y --port ${PORT}\"\n"
        )
        config = load_config(path)
        assert config.variants["m"].variants == ["b", "a"]
        assert config.variants["m"].default == "b"
        assert config.variants["m"].active == "b"
        assert config.public_names() == ["m"]

    def test_plain_configs_have_no_variants(self, tmp_config):
        config = load_config(tmp_config({"a": "x", "b": {"cmd": "y"}}))
        assert config.variants == {}
        assert config.public_names() == ["a", "b"]
        # Manually built configs (as in many tests) keep working.
        manual = Config(models={"a": ModelConfig("a", "x", 1)})
        assert manual.public_names() == ["a"]
        assert manual.resolve("a") == "a"

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"a@b": "x"}, "Model 'a@b' name must not contain '@'"),
            ({"m": {"variants": {"t@1": "x"}}},
             "Model 'm' variant 't@1' name must not contain '@'"),
            ({"m": {"variants": {}}}, "'variants' must be a non-empty mapping"),
            ({"m": {"variants": ["x"]}}, "'variants' must be a non-empty mapping"),
            ({"m": {"variants": None}}, "'variants' must be a non-empty mapping"),
            ({"m": {"variants": {"t": 5}}},
             "Model 'm' variant 't' value must be a string or mapping"),
            ({"m": {"variants": {"t": ""}}},
             "Model 'm' variant 't' has an empty command"),
            ({"m": {"variants": {"t": {"shutdown_cmd": "s"}}}},
             "Model 'm' variant 't' has an empty command"),
            ({"m": {"variants": {"t": {"cmd": "x", "shutdown_cmd": ""}}}},
             "Model 'm' variant 't' has an invalid shutdown_cmd"),
            ({"m": {"variants": {"t": {"cmd": "x", "shutdown_timeout": 3}}}},
             "Model 'm' variant 't' shutdown_timeout requires shutdown_cmd"),
            ({"m": {"variants": {"t": {
                "cmd": "x", "shutdown_cmd": "s", "shutdown_timeout": 0}}}},
             "Model 'm' variant 't' has an invalid shutdown_timeout"),
            ({"m": {"variants": {"t": {"cmd": "x", "port": 1}}}},
             "Model 'm' variant 't' has unknown key 'port'"),
            ({"m": {"variants": {"t": {"cmd": "x", "variants": {}}}}},
             "Model 'm' variant 't' has unknown key 'variants'"),
            ({"m": {"variants": {1: "x"}}},
             "Model 'm' variant name 1 (int) must be a string; put quotes"),
            ({"m": {"variants": {True: "x"}}},
             "Model 'm' variant name True (bool) must be a string; put quotes"),
            ({"m": {"variants": {"": "x"}}},
             "Model 'm' variant names must not be empty"),
            ({"m": {"default": "t"}},
             "Model 'm' has 'default' but no 'variants'"),
            ({"m": {"cmd": "x", "default": "t"}},
             "Model 'm' has 'default' but no 'variants'"),
            ({datetime.date(2024, 1, 1): "x"},
             "Model name 2024-01-01 (date) must be a string; put quotes"),
            ({"m": {"variants": {"1": "x"}, "default": 1}},
             "Model 'm' default 1 (int) is not one of its variants: '1'"),
            ({1: "x"}, "Model name 1 (int) must be a string; put quotes"),
            ({True: "x"}, "Model name True (bool) must be a string; put quotes"),
            ({None: "x"}, "Model name null must be a string; put quotes"),
            ({1.5: "x"}, "Model name 1.5 (float) must be a string; put quotes"),
            ({"m": {"variants": {"t": "x"}, "default": "v"}},
             "Model 'm' default 'v' is not one of its variants: 't'"),
            ({"m": {"variants": {"t": "x"}, "default": None}},
             "Model 'm' default null is not one of its variants: 't'"),
            ({"m": {"variants": {"t": "x"}, "cmd": "y"}},
             "Model 'm' cannot set 'cmd' next to 'variants'"),
            ({"m": {"variants": {"t": "x"}, "shutdown_cmd": "y"}},
             "Model 'm' cannot set 'shutdown_cmd' next to 'variants'"),
            ({"m": {"variants": {"t": "x"}, "shutdown_timeout": 3}},
             "Model 'm' cannot set 'shutdown_timeout' next to 'variants'"),
            ({"m": {"variants": {"t": "x"}, "extra": 1}},
             "Model 'm' has unknown key 'extra'"),
        ],
    )
    def test_invalid_variant_configs_fail_with_clear_errors(
        self, tmp_config, data, message
    ):
        with pytest.raises(ValueError) as excinfo:
            load_config(tmp_config(data))
        assert message in str(excinfo.value)


def test_commented_variants_example_in_config_example_loads(tmp_path):
    """The commented variants example in config.example.yaml is valid."""
    lines = pathlib.Path("config.example.yaml").read_text().splitlines()
    start = lines.index("# qwen3.8-27b:")
    block = []
    for line in lines[start:]:
        if not line.startswith("# "):
            break
        block.append(line[2:])
    path = tmp_path / "variants.yaml"
    path.write_text("\n".join(block) + "\n")
    config = load_config(path)
    assert config.public_names() == ["qwen3.8-27b"]
    assert config.variants["qwen3.8-27b"].variants == ["text", "vision"]
    assert config.resolve("qwen3.8-27b") == "qwen3.8-27b@text"
    assert config.models["qwen3.8-27b@vision"].shutdown_cmd == (
        "/path/to/stop-other-server"
    )


class TestReloadSupport:
    """P-20261013-04: raw spec kept for change detection; port picking."""

    def test_raw_spec_is_kept_before_port_substitution(self, tmp_config):
        path = tmp_config({
            "a": "serve --port ${PORT}",
            "b": {"cmd": "serve-b", "shutdown_cmd": "stop-b",
                  "shutdown_timeout": 5},
            "q": {"variants": {"t": "serve-t"}},
        })
        config = load_config(path)
        assert config.models["a"].spec == (
            "serve --port ${PORT}", None, 30, None
        )
        assert "${PORT}" not in config.models["a"].cmd
        assert config.models["b"].spec == ("serve-b", "stop-b", 5, None)
        assert config.models["q@t"].spec == ("serve-t", None, 30, None)
        # Code-built entries have no spec; spec does not affect equality.
        assert ModelConfig("a", "x", 1).spec is None
        assert ModelConfig("a", "x", 1) == ModelConfig("a", "x", 1, spec=(1,))

    def test_spec_holds_a_hash_of_the_script_in_script_mode(self, tmp_path):
        script = tmp_path / "run.sh"
        script.write_text("#!/bin/sh\nserve\n")
        path = tmp_path / "config.yaml"
        path.write_text(
            f"a: '{script} --name a'\n"
            f"b: '{script} --port ${{PORT}}'\n"
        )
        first = load_config(path).models["a"].spec
        assert len(first[3]) == 64
        # ${PORT} does not matter: the loader fills it in before the copy.
        assert len(load_config(path).models["b"].spec[3]) == 64
        assert load_config(path).models["b"].spec[3] == first[3]
        script.write_text("#!/bin/sh\nserve --other\n")
        second = load_config(path).models["a"].spec
        assert second[:3] == first[:3] and second[3] != first[3]

    @pytest.mark.parametrize("adjacent", (4999, 5001))
    def test_find_free_port_skips_only_reserved_ports(self, monkeypatch, adjacent):
        candidates = iter([5000, 6000, adjacent])
        monkeypatch.setattr(
            config_module, "_find_free_port", lambda: next(candidates)
        )
        assert config_module.find_free_port({5000, 6000}) == adjacent

    def test_find_free_port_without_reserved_takes_first(self, monkeypatch):
        monkeypatch.setattr(config_module, "_find_free_port", lambda: 7000)
        assert config_module.find_free_port() == 7000


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"a": {"cmd": 123}}, "Model 'a' 'cmd' must be a string, got int"),
        ({"a": {"cmd": ["x", "y"]}}, "Model 'a' 'cmd' must be a string, got list"),
        ({"a": {"cmd": None}}, "Model 'a' has an empty command"),
        ({"a": {"cmd": True}}, "Model 'a' 'cmd' must be a string, got bool"),
        (
            {"q": {"variants": {"t": {"cmd": 5}}}},
            "Model 'q' variant 't' 'cmd' must be a string, got int",
        ),
        ({"a": {"cmd": "x", "shutdown_cmd": 5}}, "Model 'a' has an invalid shutdown_cmd"),
        ({"a": {"cmd": "x", "shutdown_cmd": ["x"]}}, "Model 'a' has an invalid shutdown_cmd"),
    ],
)
def test_non_string_commands_are_rejected_with_value_error(
    tmp_config, data, message
):
    """P-20261013-04 review M1: wrong-typed commands are config errors."""
    with pytest.raises(ValueError) as caught:
        load_config(tmp_config(data))
    assert str(caught.value) == message
