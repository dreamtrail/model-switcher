# TODO

## Next

- [P-20260923-02] Split `tests/test_control_lifecycle.py` (1224 LOC) into focused test modules
- [P-20260913-04] Split `tests/test_proxy.py` (1243 LOC) into focused test modules
- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to README
- [P-20260826-03] Remove stray blank line before `handle.stopped` in `stop_process` (deferred P2 from P-20260826-01 audit)

## Discovered

- [P-20260923-04] Add a combined real-process shutdown-hook timeout/cancellation/unresolved-group/retry test; independent review found these cases covered separately, not together
- [P-20260923-05] Constrain default pytest discovery to maintained tests; root collection currently includes historical tests under ignored artifacts/
- [P-20260923-06] Develop deployment-specific DGX shutdown scripts and validate remote cleanup; hook support is complete, but real remote shutdown has not been tested (scripts assigned to other agents)

## Done

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

