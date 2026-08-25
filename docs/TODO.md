# TODO

## Next

- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to README

## Done

- [P-20260612-02] Split `tests/test_swapper.py` (1572 LOC) into focused test modules — DONE 2026-08-25 08:56 UTC
- [P-20260625-01] Remove repeated model-name prefix from upstream logs — DONE 2026-06-25 02:04 UTC
- [P-20260621-02] Add `--timestamp` CLI flag to conditionally show timestamps — DONE 2026-06-21 21:20 UTC
- [P-20260621-01] Fix vLLM log level parsing — DONE 2026-06-21 20:00 UTC

- [P-20260619-02] Render normal upstream output without INFO label — DONE 2026-06-19 04:44 UTC
- [P-20260619-01] Fix Ctrl+C shutdown hang — DONE 2026-06-19 10:45 UTC
- [P-20260616-01] Clean up upstream process log levels and format — DONE 2026-06-16 01:30 UTC
- [P-20260613-01] Release VRAM by unloading idle models — DONE 2026-06-12 16:05 UTC
- [P-20260612-08] Stop model process groups before swaps — DONE 2026-06-12 15:32 UTC
- [P-20260612-07] Auto-assign ports with temp script rewriting — DONE 2026-06-12 11:45 UTC
- [P-20260612-06] Flexible port resolution (auto-detect + explicit) — DONE 2026-06-12 11:30 UTC
- [P-20260612-05] Add color-coded log output — DONE 2026-06-12 11:19 UTC
- [P-20260612-01] Build model switcher — reverse proxy with automatic model swapping — DONE 2026-06-12 01:59 UTC
