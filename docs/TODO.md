# TODO

## NEXT

- [P-20260612-03] Add integration test for full lifecycle (start → request → swap → shutdown)
- [P-20260612-04] Add `--help` man-page style output and CLI examples to docs/CLI.md

## DISCOVERED

- [P-20261116-04] Prompt tok (P-20261116-03) review minors: docs/STATISTICS.md § Statistics calls the page column "Cache hit %" but the header says "Cache"; no end-to-end test from collector prompt count to the rendered Prompt tok cell (see `artifacts/P-20261116-03/plan.md`)
- [P-20261116-02] Favicon (P-20261116-01) review minors: `urllib` in the page_css import guard also admits `urllib.request` (top-level name check); favicon tests duplicated and don't check shape/colour; not checked in Firefox/Safari (see `artifacts/P-20261116-01/plan.md`)
- [P-20261114-02] Engine live-rate (P-20261114-01) review minors: unexpected non-HTTP error in the `/metrics` poll loop stops polling until the next first token, whose restart clears samples mid-stream (notes: artifacts/P-20261114-02/notes.md) (see `artifacts/P-20261114-01/plan.md`)
- [P-20261107-02] Stale-connection retry (P-20261107-01) review minors: `aiohttp>=3.9` floor may predate aiohttp's own idempotent retry (docs/CLI.md then overstates GET retry; raise floor or note version) (notes: artifacts/P-20261107-02/notes.md) (see `artifacts/P-20261107-01/plan.md`)
- [P-20261028-02] SSE keepalive (P-20261028-01) review minors: rare early keepalive when a new read hasn't run yet (`src/proxy.py:97-104`); bare-`\r` upstreams never get keepalives (not in docs/CLI.md) (notes: artifacts/P-20261028-02/notes.md) (see `artifacts/P-20261028-01/plan.md`)
- [P-20261027-04] Splash (P-20261027-01) not checked on a real Apple silicon Mac (docs/CONFIGURATION.md § Splash manual check); review minors: log levels, stop_grace edge cases (notes: artifacts/P-20261027-04/notes.md) (see `artifacts/P-20261027-01/plan.md`, `artifacts/P-20261027-02/plan.md`)
- [P-20261013-09] Pre-existing: at startup a non-script `${PORT}` command gets the port flag twice because `_resolve_ports` fills the port before `rewrite_command` appends `--port` (notes: artifacts/P-20261013-09/notes.md) (see `artifacts/P-20261013-07/reviewer-fix-01-port-probe.txt`)
- [P-20261013-02] Persist the chosen model variant across restarts (P-20261013-01 keeps it in memory and starts on `default`; user undecided): decide where to store it (state file vs. YAML) and how a removed variant falls back (see `artifacts/P-20261013-01/plan.md`)
- [P-20261006-02] TensorFold review nits (P-20261005-04, P-20261006-01): docs/CONFIGURATION.md and DECISIONS say `exec` lets SIGTERM reach TensorFold "directly" (inaccurate; more nits: artifacts/P-20261006-02/notes.md) (see `artifacts/P-20261005-04/review.md`, `artifacts/P-20261006-01/plan.md`)
- [P-20261005-05] TensorFold support not yet checked on real TensorFold/Apple Silicon or CUDA (docs/CONFIGURATION.md § TensorFold manual check); CUDA `tensorfold` stats keys only partly known (notes: artifacts/P-20261005-05/notes.md) (see `artifacts/P-20261005-04/plan.md`)
- [P-20261005-03] Strata review minors (P-20261005-01): `[strata] the engine reported an error: ...` logs as info because only upper-case level words count (user decision pending) (notes: artifacts/P-20261005-03/notes.md) (see `artifacts/P-20261005-01/review.md`, `review-repair.md`)
- [P-20261005-02] Strata support not yet smoke-tested on real Strata/GPU (docs/CONFIGURATION.md § Strata manual check); recipe `stop_grace: 25` (Strata waits up to 20 s for its engine) and engine SIGTERM handling unchecked (see `artifacts/P-20261005-01/plan.md`)
- [P-20260930-05] Stats tiles review minors (P-20260930-02/-03): hovering the Loads number shows the count title, not the tile explanation (notes: artifacts/P-20260930-05/notes.md) (see `artifacts/P-20260930-02/plan.md`, `artifacts/P-20260930-03/plan.md`)
- [P-20260929-07] Client disconnect before `prepare()` (while waiting for upstream headers) is still recorded as `502` with the "(pre-prepare)" label, not `client_disconnect`; also no switcher-side stall detection for relayed clients (see `artifacts/P-20260929-06/plan.md`)
- [P-20260929-04] Generation speed (P-20260929-03) review minors: server time longer than wall time clamps the aggregate interval; a later 0/0 `timings.predicted_*` pair overwrites an earlier one (notes: artifacts/P-20260929-04/notes.md) (see `artifacts/P-20260929-03/plan.md`)
- [P-20260929-02] Prompt speed (P-20260929-01) not checked against real llama.cpp/vLLM/SGLang/TabbyAPI servers; review minors: function_call/refusal-only streams lose TTFT, pre-existing `_extract_usage` misses UnicodeDecodeError (see `artifacts/P-20260929-01/plan.md`)
- [P-20260927-02] TabbyAPI support not yet smoke-tested on a real TabbyAPI server/GPU (docs/CONFIGURATION.md § TabbyAPI manual check); review minors: zero-output `prompt_progress`+finish chunk skips one TTFT sample, FORCE_COLOR defeats level parsing (see `artifacts/P-20260927-01/plan.md`)
- [P-20260926-05] Live gen t/s tile drops to `–` during prompt processing and between an agent's requests (decode counts as active only after the first token), so it can flicker with 5 s polling; user kept this behaviour for now (see `artifacts/P-20260926-04/plan.md`, review finding 2)
- [P-20260926-03] Stats table overflows at 80rem when a long model name and a 7-digit request count coincide (13 columns since P-20260930-01); it scrolls sideways and clips Prefill tok (see `artifacts/P-20260926-01/outputs/light-bignum.png`)
- [P-20260923-04] Add a combined real-process shutdown-hook timeout/cancellation/unresolved-group/retry test; independent review found these cases covered separately, not together
- [P-20260923-05] Constrain default pytest discovery to maintained tests; root collection currently includes historical tests under ignored artifacts/
- [P-20260923-06] Develop deployment-specific DGX shutdown scripts and validate remote cleanup; hook support is complete, but real remote shutdown has not been tested (scripts assigned to other agents)
