# TODO

## Next

- [P-20260930-04] Split `tests/test_stats_page.py` (1056 LOC, 44 KB) into focused test modules
- [P-20260929-05] Split `docs/DECISIONS.md` (535 lines, 75 KB): move older sections (before P-20260925) to `docs/DECISIONS_ARCHIVE.md` and link it
- [P-20260925-05] Split `src/stats.py` (1874 LOC, 67 KB): move the embedded `STATS_HTML` page into its own module; also share the page CSS that `/control` copies from `/stats` (P-20260926-02)
- [P-20260913-04] Split `tests/test_proxy.py` (1376 LOC, 51 KB) into focused test modules
- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to README
- [P-20260826-03] Remove stray blank line before `handle.stopped` in `stop_process` (deferred P2 from P-20260826-01 audit)

## Discovered

- [P-20261013-09] Pre-existing: at startup a non-script `${PORT}` command gets the port flag twice (`serve --port ${PORT}` -> `serve --port N --port N`, `serve -p ${PORT}` -> `serve -p N --port N`) because `_resolve_ports` fills the port before `rewrite_command` appends `--port`; after a reload the same entry runs with one flag (see `artifacts/P-20261013-07/reviewer-fix-01-port-probe.txt`)
- [P-20261013-08] Pre-existing: a command whose first token is an absolute path to a binary crashes startup with `UnicodeDecodeError` (script detection reads the file as text); see `artifacts/P-20261013-07/review.md`
- [P-20261013-05] SIGHUP is not handled, so it terminates the switcher: `kill -HUP` (or a systemd `ExecReload=` that sends it, so `systemctl reload`) would stop it instead of reloading. Config reload exists only through `POST /api/v1/control/reload` and the panel (P-20261013-04); optional future: handle SIGHUP as a reload trigger (see `artifacts/P-20261013-04/plan.md`)
- [P-20261013-02] Persist the chosen model variant across restarts (P-20261013-01 keeps it in memory and starts on `default`; user undecided): decide where to store it (state file vs. YAML) and how a removed variant falls back (see `artifacts/P-20261013-01/plan.md`)
- [P-20261006-02] TensorFold review nits (P-20261005-04, P-20261006-01): README/DECISIONS say `exec` lets SIGTERM reach TensorFold "directly" (it goes to the group anyway; the gain is that the tracked pid is TensorFold); `src/process_manager.py:46` is 80 chars; DECISIONS cites TensorFold `server/http.py:433` without commit 609ca419; no tests for `stream errors:` / `streamerror:` near misses (see `artifacts/P-20261005-04/review.md`, `artifacts/P-20261006-01/plan.md`)
- [P-20261005-05] TensorFold support not yet checked on real TensorFold/Apple Silicon or CUDA (README § TensorFold manual check); CUDA `tensorfold` stats keys only partly known; the switcher's 3 s SIGTERM grace may SIGKILL TensorFold while MLX saves conversation checkpoints (up to 120 s); per-model stop grace shared with P-20261005-02 (see `artifacts/P-20261005-04/plan.md`)
- [P-20261005-03] Strata review minors (P-20261005-01): `[strata] the engine reported an error: ...` logs as info because only upper-case level words count (user decision pending); recipe shows bare `python` (README notes `python3`/venv); "or another setup" in README § Strata Origin note is vague (see `artifacts/P-20261005-01/review.md`, `review-repair.md`)
- [P-20261005-02] Strata support not yet smoke-tested on real Strata/GPU (README § Strata manual check); the switcher's 3 s SIGTERM grace may SIGKILL Strata before it frees memory cleanly (Strata waits up to 20 s per stop step); consider a per-model stop grace (see `artifacts/P-20261005-01/plan.md`)
- [P-20260930-05] Stats tiles review minors (P-20260930-02/-03): hovering the Loads number shows the count title, not the tile explanation; no automated re-render test for the Loads tile; "100% of uptime" truncates at 1152–1199px; Loads/busy/time split not checked against real model servers (see `artifacts/P-20260930-02/plan.md`, `artifacts/P-20260930-03/plan.md`)
- [P-20260929-07] Client disconnect before `prepare()` (while waiting for upstream headers) is still recorded as `502` with the "(pre-prepare)" label, not `client_disconnect`; also no switcher-side stall detection for relayed clients (see `artifacts/P-20260929-06/plan.md`)
- [P-20260929-04] Generation speed (P-20260929-03) review minors: server time longer than wall time clamps the aggregate interval; a later 0/0 `timings.predicted_*` pair overwrites an earlier one; completion_time/generation_time may pair with usage from another event; n>1 buffered streams pass the 2-chunk guard; not checked on real servers (see `artifacts/P-20260929-03/plan.md`)
- [P-20260929-02] Prompt speed (P-20260929-01) not checked against real llama.cpp/vLLM/SGLang/TabbyAPI servers; review minors: function_call/refusal-only streams lose TTFT, pre-existing `_extract_usage` misses UnicodeDecodeError (see `artifacts/P-20260929-01/plan.md`)
- [P-20260927-02] TabbyAPI support not yet smoke-tested on a real TabbyAPI server/GPU (README § TabbyAPI manual check); review minors: zero-output `prompt_progress`+finish chunk skips one TTFT sample, FORCE_COLOR defeats level parsing (see `artifacts/P-20260927-01/plan.md`)
- [P-20260926-05] Live gen t/s tile drops to `–` during prompt processing and between an agent's requests (decode counts as active only after the first token), so it can flicker with 5 s polling; user kept this behaviour for now (see `artifacts/P-20260926-04/plan.md`, review finding 2)
- [P-20260926-03] Stats table overflows at 80rem when a long model name and a 7-digit request count coincide (13 columns since P-20260930-01); it scrolls sideways and clips Prefill tok (see `artifacts/P-20260926-01/outputs/light-bignum.png`)
- [P-20260923-04] Add a combined real-process shutdown-hook timeout/cancellation/unresolved-group/retry test; independent review found these cases covered separately, not together
- [P-20260923-05] Constrain default pytest discovery to maintained tests; root collection currently includes historical tests under ignored artifacts/
- [P-20260923-06] Develop deployment-specific DGX shutdown scripts and validate remote cleanup; hook support is complete, but real remote shutdown has not been tested (scripts assigned to other agents)

## Done

- [P-20261013-06] Close config reload review minors: idle-unload test, counter moves on success only, script edits count as changes, fixed-port test — DONE 2026-10-13 04:29 UTC
- [P-20261013-03] Close model variants review minors: `default` needs `variants`, names must be strings, regression tests, README wording — DONE 2026-10-13 04:29 UTC
- [P-20261013-04] Reload the config without restart (POST /api/v1/control/reload, Reload button on /control) — DONE 2026-10-13 03:13 UTC
- [P-20261013-01] Add model variants: several launch commands under one public model name, switched from /control or the API — DONE 2026-10-13 02:28 UTC
- [P-20261006-01] Log `[tensorfold] stream error:` lines as errors (user directive) — DONE 2026-10-05 15:12 UTC
- [P-20261005-04] Add TensorFold backend compatibility: `[tensorfold] ` log levels, prompt time from `tensorfold.prefill_seconds` / `prefill_s` — DONE 2026-10-05 14:48 UTC
- [P-20261005-01] Add Strata backend compatibility; stop forwarding the client `Host` to backends; `"loaded": false` health is still loading; `[strata] ` log levels — DONE 2026-10-05 11:22 UTC
- [P-20260930-03] Count and time every model load (Loads tile, `loads` JSON); `swaps` kept — DONE 2026-09-30 01:44 UTC
- [P-20260930-02] Add utilized (busy) time to the Uptime tile and `utilization` JSON — DONE 2026-09-30 01:07 UTC
- [P-20260930-01] Add prompt vs generation time split (Prompt share column, `time_split` JSON); remove cache hit meter — DONE 2026-09-30 00:38 UTC
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

