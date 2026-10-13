"""Port rewriting — auto-assign free ports and rewrite commands/scripts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

# Conservative port flag patterns (lookbehind-anchored to avoid false positives).
# Matches: --port 8000, --port=8000, --port "8000", --port '8000', --listen-port 8000
# Does NOT match: --some--port 8000, --listen-port (single dash before port)
_PORT_FLAGS_RE = re.compile(
    r"(?<!\w)--(?:listen-)?port\s*=\s*[\"']?\d+[\"']?"
    r"|"
    r"(?<!\w)--(?:listen-)?port\s+[\"']?\d+[\"']?"
)


def rewrite_command(
    cmd: str,
    port: int,
    *,
    temp_dir: Path | None = None,
    model_id: str | None = None,
    generation: int = 0,
) -> tuple[str, Path | None]:
    """Rewrite a command to use the given port.

    For script files (first token is an existing file), copies the script
    to ``temp_dir``, replaces port flags in the content, and returns the
    temp path. If ``temp_dir`` is None, appends ``--port <N>`` instead.

    For direct commands, replaces port flags in the command string.
    If no port flag is found, appends ``--port <N>``.

    Args:
        cmd: The original command string.
        port: The auto-assigned free port.
        temp_dir: Directory for temp script copies. If None, scripts get
            ``--port`` appended instead of being rewritten.
        model_id: Model id put in the temp script name, so entries that
            share one script file get separate copies.
        generation: Config reload count put in the temp script name when
            positive, so a reload never overwrites a copy that a running
            process may still read.

    Returns:
        A tuple of (rewritten_command, temp_path_or_None).
        For scripts with temp_dir, rewritten_command is the temp script path.
        For direct commands or scripts without temp_dir, temp_path is None.
    """
    port_str = str(port)

    # Fast path: ${PORT} placeholder — direct substitution, no temp file
    if "${PORT}" in cmd:
        rewritten = re.sub(r"\$\{PORT\}", port_str, cmd)
        return rewritten, None

    # Check if first token is an existing script file
    first_token = cmd.split()[0] if cmd.split() else cmd
    script_path = Path(first_token)
    if script_path.is_file() and temp_dir is not None:
        return _rewrite_script(
            script_path, cmd, port_str, temp_dir, model_id, generation
        )

    # Direct command (or script without temp_dir) — replace port flags
    rewritten = _PORT_FLAGS_RE.sub(
        lambda m: _replace_port_in_match(m.group(0), port_str),
        cmd,
    )

    # If no replacement was made, append --port
    if rewritten == cmd:
        rewritten = f"{cmd} --port {port_str}"

    return rewritten, None


def _rewrite_script(
    script_path: Path,
    cmd: str,
    port_str: str,
    temp_dir: Path,
    model_id: str | None = None,
    generation: int = 0,
) -> tuple[str, Path]:
    """Copy a script to temp_dir, replace port flags, return temp path.

    Args:
        script_path: Path to the original script file.
        cmd: The original command string (may include args after script path).
        port_str: The port number as a string.
        temp_dir: Directory to place the temp script copy.
        model_id: Optional model id added to the temp file name.
        generation: Config reload count added to the name when positive.

    Returns:
        A tuple of (full_command_with_temp_path, temp_path).
    """
    # Read script content, substitute ${PORT} and replace port flags
    content = script_path.read_text(encoding="utf-8")
    content = re.sub(r"\$\{PORT\}", port_str, content)
    had_port_flag = _PORT_FLAGS_RE.search(content) is not None
    rewritten_content = _PORT_FLAGS_RE.sub(
        lambda m: _replace_port_in_match(m.group(0), port_str),
        content,
    )

    # Create temp file with unique name (per model id when given)
    temp_path = temp_dir / _temp_script_name(
        script_path.name, model_id, generation
    )
    temp_path.write_text(rewritten_content, encoding="utf-8")
    _copy_executable_permission(script_path, temp_path)

    # Reconstruct command: temp_path + rewritten args
    parts = cmd.split()
    if len(parts) > 1:
        args = " ".join(parts[1:])
        # Rewrite port flags in args too
        args = _PORT_FLAGS_RE.sub(
            lambda m: _replace_port_in_match(m.group(0), port_str),
            args,
        )
        # Also substitute ${PORT} in args
        args = re.sub(r"\$\{PORT\}", port_str, args)
        full_cmd = f"{temp_path} {args}"
    else:
        # Script with no args — append --port only if content had no flag
        if had_port_flag:
            full_cmd = str(temp_path)
        else:
            full_cmd = f"{temp_path} --port {port_str}"

    return full_cmd, temp_path


def _temp_script_name(
    script_name: str, model_id: str | None, generation: int = 0
) -> str:
    """Name of a script's temp copy; unique per model id, reload, process.

    The id is sanitized for use in a file name; a short hash of the raw id
    keeps ids that sanitize to the same text apart. A positive reload
    generation adds an ``r<N>`` part; generation 0 (startup) adds none.
    """
    suffix = f"-r{generation}" if generation > 0 else ""
    if model_id is None:
        return f"{script_name}{suffix}-{os.getpid()}"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", model_id)[:64]
    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:8]
    return f"{script_name}-{safe}-{digest}{suffix}-{os.getpid()}"


def _replace_port_in_match(match_text: str, port_str: str) -> str:
    """Replace the port number in a matched flag with the new port.

    Handles: --port 8000, --port=8000, --port "8000", --listen-port 8080
    """
    return re.sub(r"\d+", port_str, match_text)


def create_temp_dir() -> Path:
    """Create a shared temp directory for rewritten scripts.

    Returns:
        Path to the created temp directory.
    """
    return Path(tempfile.mkdtemp(prefix="model-switcher-"))


def cleanup_temp_dir(path: Path) -> None:
    """Remove the temp directory and all its contents.

    Args:
        path: Path to the temp directory to remove.
    """
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


def _copy_executable_permission(src: Path, dst: Path) -> None:
    """Copy executable permission bits from src to dst."""
    src_stat = src.stat()
    dst_stat = dst.stat()
    exec_bits = src_stat.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    dst.chmod(dst_stat.st_mode | exec_bits)
