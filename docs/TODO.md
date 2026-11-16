# TODO

## NEXT

- [P-20260929-02] Non-UTF-8 non-streaming upstream body raises UnicodeDecodeError in `_extract_usage` (src/proxy.py:262) → 500 instead of forwarding; also catch it for SSE lines (:345); add a binary-body test (see `artifacts/P-20261116-11/explorer-01-triage.md`)
- [P-20261013-09] Startup adds `--port N` to commands whose `${PORT}` was already filled (seen on dgx1: `start.sh N --port N`); fill the port once (notes: artifacts/P-20261013-09/notes.md)
- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to docs/CLI.md
- [P-20261116-08] Variant state minors: don't log "restored" for models whose saved variant equals the default; docs/CONTROL.md:90 add the removed-variant fallback (see `artifacts/P-20261013-02/plan.md`)
- [P-20261006-02] Docs sweep: `exec` wording (CONFIGURATION.md:236,335: signal goes to the group; gain is the tracked pid); STATISTICS.md:9 "Cache hit %" → "Cache"; CLI.md bare-`\r` keepalive note and aiohttp retry version; CONFIGURATION.md:303 vague "or another setup" (notes: artifacts/P-20261006-02/notes.md)
- [P-20260923-05] Constrain default pytest discovery to maintained tests; root collection currently includes historical tests under ignored artifacts/

## DISCOVERED

- [P-20261114-02] Engine live-rate minors: an unexpected poll error stops polling until the next decode (restart clears samples); over-cap /metrics body re-read each second after a good probe (notes: artifacts/P-20261114-02/notes.md) (see `artifacts/P-20261114-01/plan.md`)
- [P-20261005-03] Backend log-level minors (owner decision for Strata): Strata lower-case `error:` logs as info; TabbyAPI with FORCE_COLOR loses levels (ANSI not stripped); Splash `Template error ·` as INFO; SIGKILL warning `%ds` truncates fractional stop_grace (notes: artifacts/P-20261005-03/notes.md)
- [P-20260929-07] Client disconnect before `prepare()` (while waiting for upstream headers) is still recorded as `502` with the "(pre-prepare)" label, not `client_disconnect`; also no switcher-side stall detection for relayed clients (see `artifacts/P-20260929-06/plan.md`)
- [P-20260929-04] Stats parsing edge cases: server decode time > wall time clamps the interval; later 0/0 `timings.predicted_*` replaces a good pair; function_call/refusal-only streams get no TTFT; TabbyAPI zero-output `prompt_progress`+finish chunk skips one TTFT (notes: artifacts/P-20260929-04/notes.md)
- [P-20260927-02] Real-hardware smoke checks (docs/CONFIGURATION.md manual checks): TabbyAPI (GPU), Strata (GPU, stop_grace 25), TensorFold (Mac/CUDA, stop_grace 130, CUDA keys), Splash (Apple silicon); prompt/gen speed on llama.cpp, SGLang, TabbyAPI (vLLM already server-timed on dgx1) (notes: artifacts/P-20260927-02/notes.md)
