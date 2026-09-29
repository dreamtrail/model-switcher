# TODO

## Next

- [P-20260929-05] Split `docs/DECISIONS.md` (416 lines, 57 KB): move older sections (before P-20260925) to `docs/DECISIONS_ARCHIVE.md` and link it
- [P-20260925-05] Split `src/stats.py` (1668 LOC, 59 KB): move the embedded `STATS_HTML` page into its own module; also share the page CSS that `/control` copies from `/stats` (P-20260926-02)
- [P-20260913-04] Split `tests/test_proxy.py` (1315 LOC) into focused test modules
- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to README
- [P-20260826-03] Remove stray blank line before `handle.stopped` in `stop_process` (deferred P2 from P-20260826-01 audit)

## Discovered

- [P-20260929-07] Client disconnect before `prepare()` (while waiting for upstream headers) is still recorded as `502` with the "(pre-prepare)" label, not `client_disconnect`; also no switcher-side stall detection for relayed clients (see `artifacts/P-20260929-06/plan.md`)
- [P-20260929-04] Generation speed (P-20260929-03) review minors: server time longer than wall time clamps the aggregate interval; a later 0/0 `timings.predicted_*` pair overwrites an earlier one; completion_time/generation_time may pair with usage from another event; n>1 buffered streams pass the 2-chunk guard; not checked on real servers (see `artifacts/P-20260929-03/plan.md`)
- [P-20260929-02] Prompt speed (P-20260929-01) not checked against real llama.cpp/vLLM/SGLang/TabbyAPI servers; review minors: function_call/refusal-only streams lose TTFT, pre-existing `_extract_usage` misses UnicodeDecodeError (see `artifacts/P-20260929-01/plan.md`)
- [P-20260927-02] TabbyAPI support not yet smoke-tested on a real TabbyAPI server/GPU (README § TabbyAPI manual check); review minors: zero-output `prompt_progress`+finish chunk skips one TTFT sample, FORCE_COLOR defeats level parsing (see `artifacts/P-20260927-01/plan.md`)
- [P-20260926-05] Live gen t/s tile drops to `–` during prompt processing and between an agent's requests (decode counts as active only after the first token), so it can flicker with 5 s polling; user kept this behaviour for now (see `artifacts/P-20260926-04/plan.md`, review finding 2)
- [P-20260926-03] Stats table overflows at 80rem when a long model name and a 7-digit request count coincide (12 columns since P-20260926-01); it scrolls sideways and clips Prefill tok (see `artifacts/P-20260926-01/outputs/light-bignum.png`)
- [P-20260923-04] Add a combined real-process shutdown-hook timeout/cancellation/unresolved-group/retry test; independent review found these cases covered separately, not together
- [P-20260923-05] Constrain default pytest discovery to maintained tests; root collection currently includes historical tests under ignored artifacts/
- [P-20260923-06] Develop deployment-specific DGX shutdown scripts and validate remote cleanup; hook support is complete, but real remote shutdown has not been tested (scripts assigned to other agents)

## Done

- [P-20260929-06] Handle client disconnect during streams: no error frame or replacement 502 after prepare, correct log label, `client_disconnect` error kind — DONE 2026-09-29 14:15 UTC
- [P-20260929-03] Use server-measured generation time (llama.cpp/TabbyAPI timings, TabbyAPI completion_time, vLLM generation_time_ms); count non-streaming requests; skip single-chunk buffered streams without server timing — DONE 2026-09-28 23:51 UTC
- [P-20260929-01] Use server-measured prompt time and cached counts for prompt t/s (llama.cpp, vLLM, SGLang, TabbyAPI); learn-per-model unknown-cache rule; first token needs real output — DONE 2026-09-28 23:30 UTC
- [P-20260927-01] Add TabbyAPI backend compatibility — DONE 2026-09-27 14:34 UTC
- [P-20260926-04] Fix live generation rate undercount (busy-time denominator, tokens-per-chunk calibration, 15 s live window) — DONE 2026-09-26 11:10 UTC
- [P-20260926-02] Make the /control page match the /stats dashboard and link to /stats — DONE 2026-09-26 09:14 UTC
- [P-20260926-01] Add live (estimated) generation rate to stats dashboard — DONE 2026-09-26 09:04 UTC
- [P-20260925-04] Add combined (busy-time) generation and prompt speed with average concurrency; token totals columns; drop Best gen t/s from page; header link to /control — DONE 2026-09-25 14:39 UTC
- [P-20260925-01] Simplify the stats dashboard page (compact table, stat tiles, light/dark card design) — DONE 2026-09-24 23:13 UTC — REVERTED 2026-09-25, RESTORED 2026-09-25 for user trial
- [P-20260923-02] Split `tests/test_control_lifecycle.py` (1224 LOC) into focused test modules — DONE 2026-09-24 13:08 UTC
- [P-20260924-01] Fix pp speed: prompt_tps excludes cached reads; expose prefill token counter — DONE 2026-09-24 12:08 UTC
- [P-20260923-03] Add optional per-model shutdown hooks with bounded execution and fail-closed cleanup — DONE 2026-09-23 13:13 UTC
- [P-20260923-01] Add same-listener runtime control panel and API — DONE 2026-09-23 03:39 UTC
- [P-20260913-03] Split statistics test coverage into focused modules — DONE 2026-09-23 01:13 UTC
- [P-20260918-04] Split `tests/test_swapper_quick_switch.py` into focused test modules — DONE 2026-09-23 00:41 UTC
- [P-20260918-03] Split `src/swapper.py` into focused modules — DONE 2026-09-23 00:14 UTC
- [P-20260920-01] Add FlashML FreeToken server compatibility — DONE 2026-09-20 05:36 UTC
- [P-20260918-05] Add per-model maximum aggregate generation throughput metric — DONE 2026-09-18 06:59 UTC
- [P-20260918-02] Add configurable quick switching for pending model loads — DONE 2026-09-18 06:00 UTC
- [P-20260916-01] Add SGLang backend compatibility — DONE 2026-09-16 02:03 UTC
- [P-20260914-01] Add cache hit rate metric (cached ÷ prompt, per model + global, dashboard column) — DONE 2026-09-14 09:58 UTC
- [P-20260913-05] Cap SSE line buffer in `stream_proxy` (64 KiB, discard non-line-shaped content) — DONE 2026-09-14 08:36 UTC
- [P-20260913-02] Add token metrics (tok/s, TTFT, TPOT) and transparent stream_options injection — DONE 2026-09-13 16:08 UTC
- [P-20260913-01] Add runtime statistics dashboard and JSON endpoint — DONE 2026-09-13 13:49 UTC
- [P-20260829-01] Add ninfer backend compatibility — DONE 2026-08-29 07:50 UTC
- [P-20260827-01] Prevent total timeout of active streams — DONE 2026-08-27 09:51 UTC
- [P-20260826-02] Add setup guide with systemd user unit and linger requirement — DONE 2026-08-26 00:44 UTC
- [P-20260826-01] Suppress duplicate "already exited" log on repeated stop — DONE 2026-08-26 00:42 UTC
- [P-20260612-02] Split `tests/test_swapper.py` (1572 LOC) into focused test modules — DONE 2026-08-25 08:56 UTC
- [P-20260625-01] Remove repeated model-name prefix from upstream logs — DONE 2026-06-25 02:04 UTC
- [P-20260621-02] Add `--timestamp` CLI flag to conditionally show timestamps — DONE 2026-06-21 21:20 UTC
- [P-20260621-01] Fix vLLM log level parsing — DONE 2026-06-21 20:00 UTC
- [P-20260619-01] Fix Ctrl+C shutdown hang — DONE 2026-06-19 10:45 UTC
- [P-20260619-02] Render normal upstream output without INFO label — DONE 2026-06-19 04:44 UTC
- [P-20260616-01] Clean up upstream process log levels and format — DONE 2026-06-16 01:30 UTC
- [P-20260613-01] Release VRAM by unloading idle models — DONE 2026-06-12 16:05 UTC
- [P-20260612-08] Stop model process groups before swaps — DONE 2026-06-12 15:32 UTC
- [P-20260612-07] Auto-assign ports with temp script rewriting — DONE 2026-06-12 11:45 UTC
- [P-20260612-06] Flexible port resolution (auto-detect + explicit) — DONE 2026-06-12 11:30 UTC
- [P-20260612-05] Add color-coded log output — DONE 2026-06-12 11:19 UTC
- [P-20260612-01] Build model switcher — reverse proxy with automatic model swapping — DONE 2026-06-12 01:59 UTC

