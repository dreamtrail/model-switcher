"""Remember the active variant of each variant model across restarts.

The state lives in ``<config stem>.state.json`` next to the config file
(``config.yaml`` -> ``config.state.json``); the YAML is never rewritten.
Format::

    {"version": 1,
     "variants": {"<model>": {"active": "<variant>",
                              "default": "<YAML default when picked>"}}}

At startup a saved pick applies only if that model's YAML ``default`` is
the same as when the pick was saved and the variant still exists;
otherwise the YAML default wins. Read and write problems are logged and
never stop the switcher or a switch.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

from src.config import Config

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_SUFFIX = ".state.json"

Entries = dict[str, dict[str, str]]


def state_path(config_path: str | Path) -> Path:
    """Path of the state file that belongs to ``config_path``."""
    path = Path(config_path)
    return path.with_name(path.stem + STATE_SUFFIX)


def _parse(data: object) -> Entries:
    """Validate the decoded file; raise ValueError on a wrong shape."""
    if not isinstance(data, dict):
        raise ValueError("top level is not an object")
    if data.get("version") != STATE_VERSION:
        raise ValueError(f"unsupported version {data.get('version')!r}")
    variants = data.get("variants")
    if not isinstance(variants, dict):
        raise ValueError("'variants' is not an object")
    entries: Entries = {}
    for name, entry in variants.items():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("active"), str)
            or not isinstance(entry.get("default"), str)
        ):
            raise ValueError(f"entry for {name!r} is not valid")
        entries[name] = {
            "active": entry["active"], "default": entry["default"]
        }
    return entries


class VariantStateStore:
    """Reads and writes one state file.

    ``_picks`` holds, for every variant model, the active variant and
    the YAML default it was started or picked under. A variant kept by
    a reload keeps that default, so a later ``default:`` edit still wins
    at the next start. ``_written`` is the content last read from or
    written to disk, used to skip writes that change nothing.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._picks: Entries = {}
        self._written: Entries | None = None

    def _read(self) -> Entries | None:
        """Entries from the file; None when missing or unusable."""
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "Variant state %s not readable (%s); using YAML defaults",
                self.path.name, exc,
            )
            return None
        try:
            return _parse(json.loads(text))
        except ValueError as exc:
            logger.warning(
                "Variant state %s is invalid (%s); using YAML defaults",
                self.path.name, exc,
            )
            return None

    def restore(self, config: Config) -> None:
        """Apply saved picks to ``config`` by the conflict rule."""
        entries = self._read()
        self._written = entries
        self._picks = {}
        for name, entry in (entries or {}).items():
            group = config.variants.get(name)
            if group is None:
                continue
            active, saved_default = entry["active"], entry["default"]
            if active not in group.variants:
                logger.warning(
                    "Variant state: %s starts on default '%s'; saved "
                    "variant '%s' no longer exists",
                    name, group.default, active,
                )
            elif saved_default == group.default:
                group.active = active
                self._picks[name] = dict(entry)
                logger.info(
                    "Variant state: %s restored to variant '%s'",
                    name, active,
                )
            elif active == group.default:
                # The default changed to the saved variant: it is both
                # the pick and the default, so nothing is ignored.
                self._picks[name] = {"active": active, "default": active}
                logger.info(
                    "Variant state: %s starts on variant '%s' (saved, "
                    "and now also the default)",
                    name, active,
                )
            else:
                logger.info(
                    "Variant state: %s starts on default '%s'; saved "
                    "variant '%s' ignored because default changed "
                    "from '%s'",
                    name, group.default, active, saved_default,
                )
        # Models without a usable saved pick start on their YAML default,
        # recorded as such (not as a pick).
        for name, group in config.variants.items():
            self._picks.setdefault(
                name, {"active": group.active, "default": group.default}
            )

    def save(self, config: Config, picked: str | None = None) -> None:
        """Write the active variant of every variant model if changed.

        ``picked`` names a model just chosen by hand; its entry records
        the current YAML default. Other models keep the default they
        were started or picked under while their variant is unchanged;
        models new since then (added by a reload) record the current one.
        Errors are logged as warnings, never raised.
        """
        entries: Entries = {}
        for name, group in config.variants.items():
            pick = self._picks.get(name)
            if (
                name != picked
                and pick is not None
                and pick["active"] == group.active
            ):
                entries[name] = dict(pick)
            else:
                entries[name] = {
                    "active": group.active, "default": group.default
                }
        self._picks = entries
        if entries == self._written:
            return
        if self._written is None and picked is None and all(
            entry["active"] == entry["default"]
            for entry in entries.values()
        ):
            # No file yet and every model is still on the default it
            # started under: the next start gets the YAML default anyway.
            return
        try:
            self._write(entries)
        except OSError as exc:
            logger.warning(
                "Could not save variant state to %s: %s",
                self.path.name, exc,
            )
            return
        self._written = entries
        logger.info(
            "Variant state saved to %s: %s",
            self.path.name,
            ", ".join(
                f"{name}={entry['active']}"
                for name, entry in entries.items()
            ) or "no variant models",
        )

    def _write(self, entries: Entries) -> None:
        """Replace the file atomically (temp file in the same folder)."""
        payload = json.dumps(
            {"version": STATE_VERSION, "variants": entries}, indent=2
        ) + "\n"
        fd, tmp = tempfile.mkstemp(
            dir=self.path.parent, prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w") as out:
                out.write(payload)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
