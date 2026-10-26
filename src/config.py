"""YAML configuration loader with automatic port assignment."""

from __future__ import annotations

import math
import re
import shlex
import socket
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.port_rewrite import script_fingerprint


@dataclass
class ModelConfig:
    """Configuration for a single model."""

    name: str
    cmd: str  # command (rewritten with auto-assigned port)
    port: int = 0  # resolved port
    shutdown_cmd: str | None = field(default=None, repr=False)
    shutdown_timeout: float = 30
    # Raw launch spec from the file (cmd before port rewriting, hook,
    # hook timeout, hash of the script file in script mode or None). A
    # config reload compares it to spot changed entries; None means
    # unknown (built in code), which always counts as changed.
    spec: tuple | None = field(default=None, repr=False, compare=False)


VARIANT_SEPARATOR = "@"


def _show(value: object) -> str:
    """Readable form of a config value for error messages.

    Strings are quoted (``'1'``), so ``1`` (int) and ``'1'`` differ; other
    values show their type, for example ``2024-01-01 (date)``.
    """
    if isinstance(value, str):
        return repr(value)
    if value is None:
        return "null"
    return f"{value} ({type(value).__name__})"


def variant_id(name: str, variant: str) -> str:
    """Return the internal model id of one variant (``name@variant``)."""
    return f"{name}{VARIANT_SEPARATOR}{variant}"


@dataclass
class VariantGroup:
    """Launch variants that share one public model name."""

    variants: list[str]  # variant names in config order
    default: str
    active: str  # runtime choice; starts at default, kept in memory only


@dataclass
class Config:
    """Parsed configuration.

    ``models`` is keyed by internal model id: the config key for plain
    entries, ``name@variant`` for each variant of a variant entry.
    ``variants`` maps a public name to its variant group.
    """

    models: dict[str, ModelConfig] = field(default_factory=dict)
    variants: dict[str, VariantGroup] = field(default_factory=dict)

    def _variant_owners(self) -> dict[str, str]:
        return {
            variant_id(name, variant): name
            for name, group in self.variants.items()
            for variant in group.variants
        }

    def public_names(self) -> list[str]:
        """Model names clients use, in config order (no variant ids)."""
        owners = self._variant_owners()
        names: list[str] = []
        for model_id in self.models:
            name = owners.get(model_id, model_id)
            if name not in names:
                names.append(name)
        return names

    def resolve(self, name: str) -> str | None:
        """Map a client model name to the internal id to run, or None.

        A variant entry resolves to its active variant. Internal variant
        ids are not public names, so they resolve to None.
        """
        group = self.variants.get(name)
        if group is not None:
            return variant_id(name, group.active)
        if name in self.models and name not in self._variant_owners():
            return name
        return None


def _find_free_port() -> int:
    """Find a free TCP port on loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as base_socket:
        base_socket.bind(("127.0.0.1", 0))
        return base_socket.getsockname()[1]


def find_free_port(reserved: set[int] = frozenset()) -> int:
    """Find a free port not already given to models in ``reserved``."""
    while True:
        port = _find_free_port()
        if port not in reserved:
            return port


def _resolve_ports(models: dict[str, ModelConfig]) -> None:
    """Auto-assign a free port for each model.

    Port rewriting (substituting ${PORT}, replacing port flags, handling
    script files) is deferred to the Swapper which manages temp file lifecycle.

    For each model:
    1. Find a free port
    2. If ${PORT} placeholder present, substitute it directly
    3. Store the resolved port
    """
    for name, mc in models.items():
        port = _find_free_port()
        mc.port = port
        # Fast path: ${PORT} placeholder — substitute directly
        if "${PORT}" in mc.cmd:
            mc.cmd = re.sub(r"\$\{PORT\}", str(port), mc.cmd)


def load_config(path: str | Path) -> Config:
    """Load and validate a YAML configuration file.

    Raises:
        FileNotFoundError: if path does not exist.
        ValueError: if config is malformed.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw = path.read_text()
    data = yaml.safe_load(raw)

    if not isinstance(data, dict):
        raise ValueError(
            f"Config must be a YAML mapping, got {type(data).__name__}"
        )

    models: dict[str, ModelConfig] = {}
    variants: dict[str, VariantGroup] = {}
    for name, value in data.items():
        if not isinstance(name, str):
            raise ValueError(
                f"Model name {_show(name)} must be a string; "
                "put quotes around the name in the YAML file"
            )
        if VARIANT_SEPARATOR in name:
            raise ValueError(
                f"Model '{name}' name must not contain "
                f"'{VARIANT_SEPARATOR}'"
            )
        if isinstance(value, dict) and "variants" in value:
            group = _parse_variant_entry(name, value, models)
            variants[name] = group
            continue
        if isinstance(value, dict) and "default" in value:
            raise ValueError(f"Model '{name}' has 'default' but no 'variants'")
        if not isinstance(value, (str, dict)):
            raise ValueError(
                f"Model '{name}' value must be a string or mapping with 'cmd', "
                f"got {type(value).__name__}"
            )
        cmd, shutdown_cmd, shutdown_timeout = _parse_launch(
            f"Model '{name}'", value
        )
        models[name] = ModelConfig(
            name=name, cmd=cmd, shutdown_cmd=shutdown_cmd,
            shutdown_timeout=shutdown_timeout,
            spec=(
                cmd, shutdown_cmd, shutdown_timeout,
                script_fingerprint(cmd),
            ),
        )

    if not models:
        raise ValueError("Config must define at least one model")

    _resolve_ports(models)
    return Config(models=models, variants=variants)


_LAUNCH_KEYS = ("cmd", "shutdown_cmd", "shutdown_timeout")
_GROUP_KEYS = ("variants", "default")


def _parse_launch(
    label: str, value: str | dict
) -> tuple[str, str | None, float]:
    """Validate one launch spec (string or mapping); return its parts."""
    cmd = value if isinstance(value, str) else value.get("cmd", "")
    # A blank ``cmd:`` (None) stays an "empty command" error, as before.
    if cmd is not None and not isinstance(cmd, str):
        raise ValueError(
            f"{label} 'cmd' must be a string, got {type(cmd).__name__}"
        )
    if not cmd:
        raise ValueError(f"{label} has an empty command")

    shutdown_cmd = None
    shutdown_timeout = 30
    if isinstance(value, dict):
        if "shutdown_cmd" in value:
            shutdown_cmd = value["shutdown_cmd"]
            try:
                args = (
                    shlex.split(shutdown_cmd)
                    if isinstance(shutdown_cmd, str) else []
                )
                valid = bool(args and args[0] and "\x00" not in shutdown_cmd)
            except ValueError:
                valid = False
            if not valid:
                raise ValueError(f"{label} has an invalid shutdown_cmd")
        if "shutdown_timeout" in value:
            shutdown_timeout = value["shutdown_timeout"]
            if shutdown_cmd is None:
                raise ValueError(
                    f"{label} shutdown_timeout requires shutdown_cmd"
                )
            try:
                valid_timeout = (
                    not isinstance(shutdown_timeout, bool)
                    and isinstance(shutdown_timeout, (int, float))
                    and math.isfinite(shutdown_timeout)
                    and shutdown_timeout > 0
                )
            except OverflowError:
                valid_timeout = False
            if not valid_timeout:
                raise ValueError(f"{label} has an invalid shutdown_timeout")
    return cmd, shutdown_cmd, shutdown_timeout


def _parse_variant_entry(
    name: str, value: dict, models: dict[str, ModelConfig]
) -> VariantGroup:
    """Validate a ``variants:`` entry; add one ModelConfig per variant."""
    label = f"Model '{name}'"
    for key in value:
        if key in _LAUNCH_KEYS:
            raise ValueError(
                f"{label} cannot set '{key}' next to 'variants'; "
                "set it inside each variant"
            )
        if key not in _GROUP_KEYS:
            raise ValueError(f"{label} has unknown key '{key}'")

    raw_variants = value["variants"]
    if not isinstance(raw_variants, dict) or not raw_variants:
        raise ValueError(
            f"{label} 'variants' must be a non-empty mapping of "
            "variant name to command"
        )

    names: list[str] = []
    for variant, spec in raw_variants.items():
        if not isinstance(variant, str):
            raise ValueError(
                f"{label} variant name {_show(variant)} must be a string; "
                "put quotes around the name in the YAML file"
            )
        if not variant:
            raise ValueError(f"{label} variant names must not be empty")
        if VARIANT_SEPARATOR in variant:
            raise ValueError(
                f"{label} variant '{variant}' name must not contain "
                f"'{VARIANT_SEPARATOR}'"
            )
        vlabel = f"{label} variant '{variant}'"
        if not isinstance(spec, (str, dict)):
            raise ValueError(
                f"{vlabel} value must be a string or mapping with 'cmd', "
                f"got {type(spec).__name__}"
            )
        if isinstance(spec, dict):
            for key in spec:
                if key not in _LAUNCH_KEYS:
                    raise ValueError(f"{vlabel} has unknown key '{key}'")
        cmd, shutdown_cmd, shutdown_timeout = _parse_launch(vlabel, spec)
        model_id = variant_id(name, variant)
        models[model_id] = ModelConfig(
            name=model_id, cmd=cmd, shutdown_cmd=shutdown_cmd,
            shutdown_timeout=shutdown_timeout,
            spec=(
                cmd, shutdown_cmd, shutdown_timeout,
                script_fingerprint(cmd),
            ),
        )
        names.append(variant)

    default = value.get("default", names[0])
    if not isinstance(default, str) or default not in names:
        raise ValueError(
            f"{label} default {_show(default)} is not one of its variants: "
            + ", ".join(repr(n) for n in names)
        )
    return VariantGroup(variants=names, default=default, active=default)
