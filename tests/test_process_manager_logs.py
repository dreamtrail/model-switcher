"""Tests for src/process_manager.py — upstream log capture and log levels.

- Upstream logs captured without repeated model-name prefixes
- _upstream_log_level(): backend log-level rules
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from src.process_manager import (
    _log_upstream_line,
    _upstream_log_level,
    start_process,
    stop_process,
)


class TestLogPiping:

    async def test_stdout_is_captured(self, caplog):
        """Process stdout is captured and logged without model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stdout.write('hello from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify the upstream output is logged plainly without model prefix.
        assert any(
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "hello from model"
            for record in caplog.records
        )

    async def test_stderr_is_captured_as_info_by_default(self, caplog):
        """Process stderr is captured as plain INFO without model prefix."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; sys.stderr.write('plain from model\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        # Verify the upstream output is logged plainly without model prefix.
        assert any(
            record.levelno == logging.INFO
            and getattr(record, "plain_upstream", False) is True
            and record.message == "plain from model"
            for record in caplog.records
        )

    async def test_stderr_warning_marker_is_captured_as_warning(self, caplog):
        """Upstream warning markers are logged as formatted WARNING."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; "
            "sys.stderr.write('13.31.779.183 W slot update_slots\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        assert any(
            record.levelno == logging.WARNING
            and getattr(record, "plain_upstream", False) is False
            and record.message == "13.31.779.183 W slot update_slots"
            for record in caplog.records
        )

    async def test_stderr_error_marker_is_captured_as_error(self, caplog):
        """Upstream error markers are logged as formatted ERROR."""
        import logging
        caplog.set_level(logging.INFO)
        handle = await start_process(
            "my-model",
            "python -c \"import sys; "
            "sys.stderr.write('13.31.779.183 E load failed\\n')\"",
            0,
        )
        await asyncio.sleep(0.3)
        await stop_process(handle)
        assert any(
            record.levelno == logging.ERROR
            and getattr(record, "plain_upstream", False) is False
            and record.message == "13.31.779.183 E load failed"
            for record in caplog.records
        )

class TestUpstreamLogLevel:
    """Unit tests for _upstream_log_level() regex matching."""

    # --- vLLM format ---

    def test_vllm_info_full_line(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) INFO 06-21 20:55:26 [gpu_model_runner.py:6585] message"
        )
        assert level == logging.INFO

    def test_vllm_warning(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) WARNING 06-21 20:55:27 [model.py:1502] Default parameters"
        )
        assert level == logging.WARNING

    def test_vllm_error(self):
        level = _upstream_log_level(
            "(EngineCore pid=16894) ERROR 06-21 20:55:26 [core.py:306] load failed"
        )
        assert level == logging.ERROR

    def test_vllm_api_server_info(self):
        level = _upstream_log_level(
            "(APIServer pid=16821) INFO 06-21 20:55:26 [api_server.py:579] ready"
        )
        assert level == logging.INFO

    def test_vllm_no_level_keyword(self):
        """Line with component prefix but no level keyword defaults to INFO."""
        level = _upstream_log_level(
            "(EngineCore pid=16894) Loading model weights..."
        )
        assert level == logging.INFO

    # --- llama.cpp format (regression) ---

    def test_llama_warning_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 W slot update_slots")
        assert level == logging.WARNING

    def test_llama_error_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 E load failed")
        assert level == logging.ERROR

    def test_llama_info_with_timestamp(self):
        level = _upstream_log_level("13.31.779.183 I model loaded")
        assert level == logging.INFO

    def test_llama_info_at_position_zero(self):
        level = _upstream_log_level("INFO [init] initializing slots")
        assert level == logging.INFO

    def test_llama_warn_at_position_zero(self):
        level = _upstream_log_level("WARN [update_slots] all slots are idle")
        assert level == logging.WARNING

    # --- edge cases ---

    def test_plain_text_defaults_to_info(self):
        level = _upstream_log_level("some plain text with no markers")
        assert level == logging.INFO

    def test_level_keyword_midline_not_matched(self):
        """Level keyword in message body should not be matched."""
        level = _upstream_log_level("This WARNING is part of the message")
        assert level == logging.INFO

    # --- missing severity markers ---

    def test_f_single_letter_error(self):
        """Single-letter F maps to ERROR."""
        level = _upstream_log_level("F fatal error occurred")
        assert level == logging.ERROR

    def test_fatal_keyword(self):
        """FATAL keyword maps to ERROR."""
        level = _upstream_log_level("FATAL system crash")
        assert level == logging.ERROR

    def test_critical_keyword(self):
        """CRITICAL keyword maps to ERROR."""
        level = _upstream_log_level("CRITICAL disk failure")
        assert level == logging.ERROR

    # --- empty / whitespace inputs ---

    def test_empty_string_defaults_to_info(self):
        """Empty string defaults to INFO."""
        level = _upstream_log_level("")
        assert level == logging.INFO

    def test_leading_whitespace_before_level(self):
        """Leading whitespace before level keyword is handled."""
        level = _upstream_log_level("  WARNING delayed startup")
        assert level == logging.WARNING

    # --- case insensitivity ---

    def test_lowercase_level_after_vllm_prefix(self):
        """Lowercase level keyword after vLLM prefix matches."""
        level = _upstream_log_level("(EngineCore pid=1) info 06-21 20:55:26 [core.py] msg")
        assert level == logging.INFO

    def test_warn_after_vllm_prefix(self):
        """WARN keyword (not WARNING) after vLLM prefix matches."""
        level = _upstream_log_level("(EngineCore pid=1) WARN 06-21 20:55:27 [model.py] msg")
        assert level == logging.WARNING

    # --- TabbyAPI format (P-20260927-01) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("12:34:56.789 WARNING:  Port 5000 in use", logging.WARNING),
            ("12:34:56.789 ERROR:    Chat completion aborted", logging.ERROR),
            ("12:34:56.789 CRITICAL: out of memory", logging.ERROR),
            ("12:34:56.789 SUCCESS:  Model loaded", logging.INFO),
            ("12:34:56.789 DEBUG:    request body", logging.INFO),
            ("12:34:56.789 TRACE:    sampler state", logging.INFO),
            ("12:34:56.789 INFO:     Loading model", logging.INFO),
            ("WARNING:  Disabling authentication", logging.WARNING),
            ("CRITICAL: fatal", logging.ERROR),
            ("SUCCESS:  ready", logging.INFO),
            ("ERROR:", logging.ERROR),
        ],
    )
    def test_tabby_level_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        "line",
        [
            # Level inside the message body of an INFO record.
            "12:34:56.789 INFO:     client sent WARNING: nothing",
            # Case-sensitive: lowercase loguru names are not TabbyAPI markers.
            "12:34:56.789 warning: lower case",
            # Level must be directly followed by a colon.
            "12:34:56.789 WARNING  missing colon",
        ],
    )
    def test_tabby_prefix_does_not_promote_other_lines(self, line):
        assert _upstream_log_level(line) == logging.INFO


    # --- Strata format (P-20261005-01) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Verbatim shapes from Strata serve/server.py @ 6f32ec0.
            ("[strata] still starting (42 s) - please wait ...", logging.INFO),
            ("[strata] reading the prompt: 2048 tokens, 3.1 s so far",
             logging.INFO),
            ("[strata] done: 128 tokens in 4.2 s (30.5 tok/s)", logging.INFO),
            ("[strata] refused a request for Host 'dgx1:8080'", logging.INFO),
            ("[strata] stopping ...", logging.INFO),
            ("[strata] WARNING: RAM is tight - 3.2 GiB free", logging.WARNING),
            ("[strata] WARN: low VRAM headroom", logging.WARNING),
            ("[strata] ERROR: engine exited with code 1", logging.ERROR),
            ("[strata] FATAL: could not open the model", logging.ERROR),
            ("[strata] CRITICAL: out of memory", logging.ERROR),
            ("[strata] WARNING: retry after ERROR in engine", logging.ERROR),
            # Lower-case words are message text, not levels.
            ("[strata] the engine reported an error: bad request",
             logging.INFO),
            ("[strata] a warning about nothing", logging.INFO),
            # Word boundaries: no partial matches.
            ("[strata] WARNINGS_SEEN=0 ERRORS=0", logging.INFO),
        ],
    )
    def test_strata_line_levels(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Not "[strata] "-prefixed: the existing rules apply unchanged.
            ("loading the model ...", logging.INFO),
            ("       WARNING: no API key - anyone on this machine can use it",
             logging.WARNING),
            ("ERROR something failed", logging.ERROR),
            ("[strata]WARNING: no space after the tag", logging.INFO),
            ("12:34:56.789 INFO:     [strata] WARNING: quoted", logging.INFO),
        ],
    )
    def test_strata_rule_needs_exact_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    # --- TensorFold format (P-20261005-04) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Shapes from TensorFold src/tensorfold @ 609ca419.
            ("[tensorfold] serving qwen at http://127.0.0.1:8080/v1 "
             "(mlx, loaded in 12.3s)", logging.INFO),
            ("[tensorfold] done req-0123456789ab prompt=300 cached=256 "
             "tokens=3 tok/s=100.0 ttft=0.10s prefill=0.09s rounds=2 "
             "accepted=1/2", logging.INFO),
            ("[tensorfold] warning: non-loopback server has no API key; "
             "requests are open", logging.WARNING),
            ("[tensorfold] WARNING: prefill kernel pin mismatch",
             logging.WARNING),
            ("[tensorfold] Warning: mixed case", logging.WARNING),
            ("[tensorfold] request error: RuntimeError: boom",
             logging.ERROR),
            ("[tensorfold] REQUEST ERROR: upper case", logging.ERROR),
            ("[tensorfold] error: plain error", logging.ERROR),
            ("[tensorfold] Error: mixed case", logging.ERROR),
            # Only the start of the message counts.
            ("[tensorfold] the client saw an error: bad request",
             logging.INFO),
            ("[tensorfold] retry after warning: none", logging.INFO),
            ("[tensorfold] retry after stream error: boom", logging.INFO),
            ("[tensorfold] stream error: RuntimeError: boom", logging.ERROR),
            ("[tensorfold] STREAM ERROR: upper case", logging.ERROR),
            ("[tensorfold] snapshot save failed: OSError: disk full",
             logging.INFO),
            # A colon is required right after the word.
            ("[tensorfold] warnings=0 errors=0", logging.INFO),
            ("[tensorfold] error count 0", logging.INFO),
            ("[tensorfold]  warning: two spaces after the tag",
             logging.INFO),
        ],
    )
    def test_tensorfold_line_levels(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Not "[tensorfold] "-prefixed: the existing rules apply.
            ("[tensorfold]warning: no space after the tag", logging.INFO),
            ("[TensorFold] warning: other case tag", logging.INFO),
            ("  [tensorfold] error: indented", logging.INFO),
            # Traceback lines on stderr carry no prefix.
            ("Traceback (most recent call last):", logging.INFO),
            ("RuntimeError: boom", logging.INFO),
            ("WARNING: generic rule still applies", logging.WARNING),
            ("ERROR something failed", logging.ERROR),
        ],
    )
    def test_tensorfold_rule_needs_exact_prefix(self, line, expected):
        assert _upstream_log_level(line) == expected

    def test_tensorfold_info_lines_stay_plain_upstream(self, caplog):
        caplog.set_level(logging.INFO, logger="src.process_manager")
        _log_upstream_line("[tensorfold] done req-1 prompt=3 tokens=1")
        _log_upstream_line("[tensorfold] request error: RuntimeError: boom")
        info, error = caplog.records[-2:]
        assert info.levelno == logging.INFO
        assert getattr(info, "plain_upstream", False) is True
        assert error.levelno == logging.ERROR
        assert getattr(error, "plain_upstream", False) is False

    # --- Splash format (P-20261027-01) ---

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Shapes from Splash server/*.py and runtime/*.mm @ 39a9738:
            # print_status / logLine write "HH:MM:SS <event> · <details>".
            ("12:34:56 Loading · owner/model-4bit", logging.INFO),
            ("12:34:56 Chat template · model's own", logging.INFO),
            ("12:34:56 Ready · owner/model-4bit · context 32,768 · "
             "http://127.0.0.1:8000", logging.INFO),
            ("12:34:56 Started · input 1,200 · cached 1,024", logging.INFO),
            ("12:34:56 Done · input 1,200 · cached 1,024", logging.INFO),
            ("12:34:56 Cancelled · input 10 · cached 0", logging.INFO),
            ("12:34:56 Stopping · releasing engine resources", logging.INFO),
            ("12:34:56 Engine restarted", logging.INFO),
            ("12:34:56 Persistent cache: stopping before every newest "
             "restore point reached the disk.", logging.INFO),
            ("12:34:56 Error · 404 · GET /v1/nope", logging.ERROR),
            ("12:34:56 Error · internal_server_error · KeyError · "
             "server/server.py:123", logging.ERROR),
            ("12:34:56 Error · unable to start HTTP server: [Errno 48] "
             "Address already in use", logging.ERROR),
            ("12:34:56 Engine failed · native runtime exited", logging.ERROR),
            ("12:34:56 Engine restart failed · no memory", logging.ERROR),
            ("12:34:56 Engine stopped · the inference engine failed 3 times "
             "in a row", logging.ERROR),
            ("12:34:56 Warning · --allowed-origin '*' without --api-key "
             "lets every web page use this server", logging.WARNING),
            ("12:34:56 Refused · Origin https://example.com · restart with "
             "--allowed-origin", logging.WARNING),
            ("12:34:56 Refused · further Origins are not logged",
             logging.WARNING),
            ("--:--:-- Warning · Metal command buffer retried",
             logging.WARNING),
            ("12:34:56 Error ·", logging.ERROR),
            # Native runtime exit lines (runtime main.mm) are plain
            # "error: ..." and already match the generic rule.
            ("error: native transport stopped after a protocol failure",
             logging.ERROR),
            ("error: the Metal backend is unhealthy; exiting without "
             "teardown", logging.ERROR),
        ],
    )
    def test_splash_line_levels(self, line, expected):
        assert _upstream_log_level(line) == expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            # Only the event right after the time counts, exactly.
            ("12:34:56 Done · input 3 · Error · late", logging.INFO),
            ("12:34:56 Errors · 3", logging.INFO),
            ("12:34:56 error · lower case", logging.INFO),
            ("12:34:56 Error: no middle dot", logging.INFO),
            ("12:34:56 Error ·no space", logging.INFO),
            ("12:34:56 Warning: no middle dot", logging.INFO),
            (" 12:34:56 Error · indented", logging.INFO),
            ("12:34:56.789 Error · millisecond time", logging.INFO),
            # Other lines keep the existing rules (a leading "Error" word
            # was already an error by the generic rule).
            ("Error · no time", logging.ERROR),
            ("12:34:56.789 ERROR:    boom", logging.ERROR),
            ("12:34:56.789 WARNING:  careful", logging.WARNING),
            ("12:34:56.789 INFO:     Started server", logging.INFO),
            ("13.31.779.183 W warmup slow", logging.WARNING),
            ("[tensorfold] warning: x", logging.WARNING),
            ("[strata] ERROR: x", logging.ERROR),
        ],
    )
    def test_splash_rule_needs_exact_shape(self, line, expected):
        assert _upstream_log_level(line) == expected

    def test_splash_info_lines_stay_plain_upstream(self, caplog):
        caplog.set_level(logging.INFO, logger="src.process_manager")
        _log_upstream_line("12:34:56 Done · input 3 · cached 0")
        _log_upstream_line("12:34:56 Refused · Origin https://a.example")
        info, warning = caplog.records[-2:]
        assert info.levelno == logging.INFO
        assert getattr(info, "plain_upstream", False) is True
        assert warning.levelno == logging.WARNING
        assert getattr(warning, "plain_upstream", False) is False

