# Decisions

## P-20260612-01 — Model Switcher

### Architecture
- **Reverse proxy** (transparent to clients) over CLI-driven or hybrid. Clients point `base_url` to the switcher; no client changes needed.
- **Catch-all proxy** — proxy any request to the active upstream. Extract `model` from request body when present. `/v1/models` returns static config list. Future-proof against new endpoints.
- **One model at a time** — simpler, predictable, matches user's use case. Concurrency can be added later.

### Configuration
- **YAML** over JSON — more readable for command strings, supports comments, matches llama-swap convention.
- **Dynamic `${PORT}`** — switcher auto-assigns free ports at runtime. No port conflicts, simpler for user.

### Health Checking
- **Poll `/health` first, fallback to `/v1/models`** — verified that `llama-server` supports `/health` (503 while loading, 200 when ready). Fallback for non-llama.cpp servers. 500ms interval, 120s timeout.

### Swap Behavior
- **Hold incoming connection during swap** — client waits, no manual retry. Swap timeout: **60s** (configurable).
- **Queue up to 10 requests during swap**, reject beyond with 503. Balances common case (1-2 requests) with bounded memory.

### Process Management
- **SIGTERM + 3s grace → SIGKILL** — clean shutdown with fast fallback. 3s chosen over 10s for responsiveness.
- **No auto-restart on crash** — return 502 to client. Operator sees the error and investigates. Switcher marks model as stopped; next request triggers fresh start.

### Idle Unload
- **Default idle unload: 30m** — stop the active model after 30 minutes without upstream/model traffic to release VRAM.
- **Configurable via `--idle-timeout`** — accepts bare seconds or integer `s`/`m`/`h` suffixes (`900`, `900s`, `15m`, `1h`); `0` disables.
- **Idle begins after the last in-flight model request finishes** — prevents unloading during long or streaming responses.
- **Only proxied model traffic counts as activity** — explicit model requests and no-model requests routed to the active upstream reset idle; switcher-local `/v1/models` and `/models` do not.
- **Idle unload clears active model state** — a later no-model request returns the existing 400; a later request with `model` restarts normally.

### Observability
- **Stdout only** — proxy logs + upstream logs. User can redirect at shell level. No file/JSON logging in v1.
- **User-facing CLI format omits Python logger names** — log lines include timestamp, level, and message, without module names such as `src.process_manager`.
- **Upstream logs omit repeated model prefixes** — captured upstream output is emitted as the server wrote it; model identity remains in lifecycle messages such as start/stop/swap events.
- **Stderr does not imply warning** — upstream stdout/stderr lines default to INFO unless common severity markers indicate WARNING or ERROR.

### Streaming
- **Chunked SSE passthrough** — only correct choice for chat completions. Read/write chunks in real-time via aiohttp async streaming.
- **No total deadline for active streams** — for positive `--timeout` values, streaming requests use no total wall-clock timeout; connection/pool acquisition and each wait for upstream data remain bounded by `--timeout`.
- **Keep total deadlines for swaps and non-stream requests** — only streaming overrides the shared session timeout. This allows long generations while data flows without allowing stalled connections or reads to remain open indefinitely. Non-positive timeout semantics remain unchanged and unspecified.

### Framework
- **`aiohttp` only** (single dep) — provides both async HTTP server and client. No need for FastAPI-level features.

### CLI
- `--config` (required), `--host` (default `127.0.0.1`), `--port` (default `8080`), `--timeout` (default `60`), `--idle-timeout` (default `30m`), `--version`
- No auth or TLS flags in v1.

### Project Structure
- `src/` package layout per project conventions (not `model_switcher/`).

### Out of Scope (v1)
- Concurrent model running (matrix)
- API key auth / TLS
- Docker/Podman `cmdStop`
- Model aliases
- Request filters (stripParams, setParams)
- Web UI
- Metrics endpoint

## P-20260829-01 — NInfer Compatibility

### Backend Integration
- **Use the generic foreground-command contract** — NInfer already accepts an assigned `--port`, binds to loopback, reports post-warm-up readiness on `/health`, serves compatible HTTP APIs, and handles SIGTERM. A backend adapter or new config schema would add no required behavior.
- **Use embedded model IDs unchanged** — each config key and request `model` must equal the artifact's public ID. Do not pass NInfer's optional `--model-id` override and do not rewrite request bodies.
- **Treat the published IDs as version-sensitive documentation** — current IDs are documented for users but are not hard-coded as an allowlist, so future NInfer artifacts remain usable through the generic command interface.

### Output Compatibility
- **Proxy NInfer output opaquely** — Chat Completions JSON and `[DONE]` SSE, Responses API named SSE events, upstream errors, and response headers use the existing passthrough behavior without transformations.
- **Classify NInfer logs by their outer marker** — preserve complete `[timestamp] [level] ninfer-serve: ...` lines while mapping `warning` and `error` markers to switcher severity. Words inside an outer `[info]` message do not promote its level.

### Constraint
- **One configured artifact per embedded ID** — NInfer's groupwise and NVFP4 artifacts for each 27B family share an ID. Without aliases, duplicate variants cannot be selected simultaneously; aliases and model rewriting remain out of scope.

## P-20260913-01 / P-20260913-02 — Statistics & Token Metrics

### Collection & Presentation
- **Single-writer `StatsCollector` in `src/stats.py`** — swapper reads via `snapshot()`; `proxy.py` communicates only through the `Outcome` value object (no proxy→stats coupling). All state mutated synchronously on the asyncio loop; no locks.
- **Two explicit endpoints: `/stats` (HTML dashboard) and `/stats/json`** — content negotiation rejected as brittle. Both switcher-local intercepts before the body read, mirroring `/v1/models`; they shadow upstreams serving the same paths (accepted, documented).
- **Always-on, flag-free** — endpoints cost nothing when unused; opt-in flags would be speculative.
- **Embedded static HTML + vanilla JS** — fetches `/stats/json` every 5 s, `textContent`-only rendering (no innerHTML), null-safe "–" display. No templating, no build step, no new dependencies.
- **In-memory only; reset on restart** — persistence is a future addition, not now.

### Token & Latency Metrics (P-20260913-02, conventions from llama-swap/vLLM/LiteLLM/llama.cpp research)
- **Exact token counts only** — from the upstream OpenAI `usage` field; never estimate via chars/4. Absent usage renders as "–".
- **Transparent `stream_options: {"include_usage": true}` injection** — POST + `/v1/chat/completions` + `/v1/completions` + JSON dict + `stream: true` + client `stream_options` absent; falls back to original body on any failure; forwarded `content-length` dropped so aiohttp recomputes it.
- **"tokens/second" is always two rates** — prompt-processing t/s (prefill; uncached prompt work only) and generation t/s (decode), token-weighted aggregates (Σtokens/Σtime), never blended.
- **Prefill excludes cached reads (P-20260924-01)** — pp work is uncached prompt only: `max(prompt − cached, 0)` when the backend reports `cached_tokens`, the full `prompt` otherwise. Fully-cached requests and clamped cumulative counts (`cached > prompt`) contribute zero work and no time, so cache-heavy workloads never dilute the average. `cache_hit_rate` keeps its no-clamping trust model; negative cached counts stay unclamped and can inflate prefill (exact counts as reported).
- **Timing metrics are streaming-only** — TTFT (proxy start → first content event, percentiles) and TPOT (`decode_time/(completion_tokens−1)`, requires ≥2 completion tokens). Non-streaming requests contribute token counters only; no timing split is observable.
- **Latency excludes queue wait and swap/model-load time** — measured around the proxy chokepoint only; documented as upstream-round-trip latency.
- **Outcome classification**: success = final status < 400 with no mid-stream error; errors keyed by kind (`400`/`404`/`502`/`503`/`504`/`stream_error`/`error`); swap = any stop-then-start in `_ensure_model` incl. same-model restarts and crash recovery, duration only on successful health-readiness.

## P-20260913-05 / P-20260914-01 — SSE Buffer Cap & Cache Hit Rate

### Streaming Robustness (P-20260913-05)
- **Cap the pending SSE line buffer at 64 KiB** (`_SSE_LINE_BUFFER_CAP`) — a newline-free stream (e.g. binary audio) is not line-shaped SSE; the split remainder is discarded once it exceeds the cap. Complete lines are never discarded; byte counts stay exact via `bytes_out`.
- **No config surface, no content-type gating** — the cap alone fixes the unbounded-growth finding; gating would change stats behavior more broadly.
- **Trade-off:** token metrics are absent for a discarded pathological segment (such streams never yield usage anyway); a legitimate single SSE line > 64 KiB would lose metrics (accepted for a local dev tool).

### Cache Hit Rate (P-20260914-01)
- **`cache_hit_rate = cached_tokens ÷ prompt_tokens`** — token-weighted lifetime ratio since process start, per model and global; computed in the shared `_requests_report()` so one edit covers both reports. Denominator 0 → `null` (same None-guard pattern as existing rates).
- **Bare scalar in the `tokens` section** (not the `rates` object) — it is a derived ratio over counters, not a time-windowed rate; no windowed variants (lifetime only, matching `prompt_tps`).
- **No clamping** — backends reporting cumulative/oversized cached counts yield ratios > 1, reported as-is (exact counts only, never estimate).
- **Per-stream breakdown rejected** — user decision: keep aggregation as-is; per-request data exists at the `record_request` chokepoint if ever revisited.

## P-20260916-01 — SGLang Compatibility

### Backend Integration
- **Reuse the generic foreground-command contract** — SGLang v0.5.19 supports `sglang serve`, assigned host/port flags, OpenAI-compatible HTTP/SSE, and process-group shutdown. No backend adapter, dependency, registry, command wrapper, or special log parser is needed.
- **Align model IDs at launch** — the config key, SGLang's `--served-model-name`, and the request `model` must match. Model-switcher continues to forward model IDs unchanged rather than adding aliases or rewriting request bodies.
- **Limit certification to OpenAI-compatible routes** — Chat Completions JSON/SSE is supported, including `stream_options.include_usage`. Responses API named-event passthrough is supported only when the selected SGLang version and model expose that optional endpoint. SGLang-native routes such as `/generate` remain out of scope.

### Readiness
- **Treat an implemented `/health` as authoritative** — SGLang exposes HTTP routes during warm-up: `/health` returns 503 while `/v1/models` can already return static metadata with 200. The primary health probe therefore does not follow redirects, and loading, unhealthy, authentication, server-error, and redirect statuses cannot be overridden by model discovery.
- **Fallback only when `/health` is unsupported** — original 404 or 405 responses permit the existing `/v1/models` fallback. The fallback request retains its existing redirect behavior for servers without a health endpoint.

### Verification Trade-off
- **Use deterministic loopback fixtures in automation** — real model loading requires platform-specific SGLang installation, a compatible accelerator, large model artifacts, and material runtime. Tests exercise the real model-switcher config, readiness, and proxy paths against SGLang-shaped fixtures; README documents a non-gating hardware-backed smoke check.

## P-20260918-01 — Pending Load Supersession (Superseded)

This proposal made pending-load supersession unconditional. It was superseded before user approval or implementation by P-20260918-02, which makes the behavior configurable. The cleanup-safety reasoning below was retained in the governing design, but this section does not describe current or planned behavior on its own.

### Historical Proposal
- A newer admitted request for a different model would supersede a pending load; the latest model would win, same-model requests would join, and obsolete requests would receive HTTP 409 `swap_superseded`.
- Supersession would interrupt readiness only, preserving safe process-spawn, teardown, and active-proxy boundaries.
- Replacement would require confirmed process-group cleanup; unconfirmed cleanup would fail closed with HTTP 503 `process_cleanup_failed`.

## P-20260918-02 — Configurable Quick Switching

### Configuration and Request Contract
- **CLI-only opt-in** — `--quick-switch` is a boolean flag that defaults off. The flat YAML model schema is unchanged.
- **Quick switch off** — requests for the model already loading join its bounded load. A different-model request is not queued and cannot mutate intent or readiness; it receives HTTP 409 with `error.code` set to `switch_in_progress`.
- **Quick switch on** — the latest admitted different-model target wins, while same-target requests join. Obsolete requests receive HTTP 409 with `error.code` set to `swap_superseded`.
- **Share start failures** — if process creation fails, the owner and same-target joiners receive HTTP 500 with `error.code` set to `model_start_failed`. The failed load is not retried automatically; a later independent request may retry.

### Lifecycle and Cleanup Safety
- **Interrupt readiness only** — supersession may cancel readiness for a started-but-unhealthy process, but not spawn, teardown, a healthy active proxy request, or other lifecycle work at unsafe boundaries.
- **Confirm cleanup before replacement** — retain graceful SIGTERM, the existing grace period, and SIGKILL escalation. No replacement starts until the tracked parent/process group is confirmed absent.
- **Fail closed** — unconfirmed cleanup returns HTTP 503 `process_cleanup_failed`, retains the unresolved process handle, and permits no replacement. Operator intervention may be required for an unkillable process.
- **Own asynchronous lifecycle tasks explicitly** — spawn, readiness, and cleanup tasks must reach defined terminal states so external request cancellation cannot orphan a returned process or leave waiters hanging. A failed cancellation cleanup remains tracked and blocks replacement.
- **Preserve shutdown statistics** — confirmed non-shutdown lifecycle stops are counted; shutdown termination, including a successful final retry, remains excluded from model-stop statistics.

### Constraints and Rejected Alternatives
- Keep the existing single-model architecture and bounded admission. Do not add a cancellation endpoint, dedicated worker, YAML setting, dependency, immediate-SIGKILL policy, or cancellation of healthy active proxy requests.
- **Status** — implemented under P-20260918-02. The governing behavior is verified by deterministic lifecycle/race tests, the full automated suite, and independent implementation/test audits.

## P-20260918-05 — Maximum Aggregate Generation Throughput

### Measurement Contract
- **Per-model completed cohorts only** — `models.<name>.requests.rates.generation_tps.max_aggregate` is either `null` or `{"tps": number, "concurrency": integer}`. The global generation-rate object remains unchanged. The dashboard renders this as **Max Agg Gen t/s**, using one decimal and `@` (for example, `107.8@3`), or `–` before data exists. (Renamed to **Best gen t/s**, rendered `107.8 @3`, under P-20260925-01.)
- **Exact cohort rate** — a cohort is a maximal connected component of positively overlapping decode intervals. Its rate is the sum of exact completion tokens divided by the wall-clock span from the earliest first token to the latest final stream end; concurrency is the peak simultaneous decode count.
- **Half-open intervals** — decode intervals are `[first token, stream end)`, so intervals that merely touch are separate and do not increase concurrency. Failed, cancelled, non-streaming, usage-less, zero-token, and nonpositive-duration requests are excluded and cannot bridge eligible cohorts.
- **Strict lifetime maximum** — compare unrounded rates and replace only on a strict increase; equal rates keep the earlier result. State is per model, in memory, and resets with the process.

### Lifecycle Boundary
- **Observe without coupling the proxy to statistics** — the proxy reports the existing first-token timestamp through an optional synchronous outcome observer. The request lifecycle owns an opaque identity and always closes a started decode from its `finally` path; the statistics collector owns interval state, eligibility, cohort finalization, and reporting.
- **Calculate after completion** — exact usage and final success are known only when a stream ends. Ended eligible components publish once no active interval can overlap or bridge them; unrelated non-overlapping activity does not delay publication.

### Accepted Exactness Trade-off
- **Preserve exact maximal cohorts rather than cap or approximate them** — the L4 interview outcome accepted delayed publication and potentially growing interval state while a continuously overlapping cohort remains unresolved. A cap, rolling window, average-times-concurrency estimate, or chunk/token estimate was rejected because it would change the requested metric's meaning. Once the overlap closes, temporary cohort state is finalized and only the per-model maximum remains.

## P-20260920-01 — FreeToken v0.1.3 Compatibility

### Generic Backend Integration
- **Reuse the generic foreground-command contract** — FreeToken v0.1.3 at release commit `cac247a860e316e06580d05aeb05f2e647bde214` accepts `ft serve`, an assigned loopback host/port, a public `--served-model-name`, HTTP/SSE requests, and process-group termination. The config key, served model name, and request `model` remain identical; no backend registry, command detection, dependency, config field, alias, or request-body rewrite is needed.
- **Keep lifecycle management generic** — `ft` must be available in the switcher's execution environment and run in the foreground. Existing SIGTERM, grace-period, and forced-stop process-group handling owns swaps and shutdown; FreeToken's shell, desktop, and control-plane workflows are outside this integration.

### Readiness and Port Selection
- **Interpret only the pinned lifecycle values** — an HTTP 200 `/health` body with exact JSON status `ok` is ready, while `loading` or `error` remains pending without `/v1/models` fallback. Unknown, missing, malformed, and non-object 200 bodies preserve the generic ready behavior; non-200 handling and 404/405-only fallback remain unchanged. Fatal FreeToken health therefore uses the existing timeout and cleanup path instead of a new error contract.
- **Probe the adjacent port for every model** — FreeToken derives its internal distributed address from API port + 1, so automatic selection holds successful loopback binds for base and base + 1 simultaneously and rejects base 65535. Both probes close before startup, retaining the accepted race in dynamic assignment. Applying this generic two-port probe avoids backend inference or schema expansion.

### Certified Transport Boundary
- **Certify a representative four-route matrix, not protocol conformance** — `/v1/chat/completions` and `/v1/completions` cover JSON plus `data:` SSE, injected `stream_options.include_usage`, a usage chunk, and terminal `[DONE]`; `/v1/messages` covers Anthropic JSON plus named SSE through `message_stop` without `[DONE]`; `/v1/responses` covers Responses JSON plus named SSE through representative `response.completed` without `[DONE]`. Statuses and headers remain transparent, with no FreeToken-specific wire transformation.
- **Keep upstream limitations explicit** — the v0.1.3 OpenAI implementation rejects some optional fields and constrained response formats, while its Responses API is stateless and does not support background mode, prior-response continuation, retrieval, or cancellation. Compatibility does not imply support for every optional OpenAI, Anthropic, or Responses parameter.

### Verification Trade-off
- **Use pinned deterministic loopback fixtures in automation** — tests derive representative payloads from the immutable v0.1.3 sources and exercise the real config, health, switcher, and proxy paths without importing FreeToken. A live gate would require Linux x86_64, Python 3.10+, an NVIDIA GPU with driver r580+, CUDA 13 and `nvcc`, compatible model weights, substantial memory, and first-use compilation, so README documents an optional non-gating hardware smoke procedure instead.

## P-20260923-01 — Same-Listener Control Panel and Runtime API

### State and Interface Ownership
- **Keep one listener and one lifecycle owner** — `src/control.py` owns the embedded panel, reserved HTTP namespace, validation, and response mapping. `Swapper` remains the sole writer of lifecycle, admission, idle-timer, shutdown, and runtime-setting state; control code never starts or stops a process directly.
- **Use a versioned synchronous API** — `/control` and `/api/v1/control/*` intentionally shadow matching upstream routes. Model load/switch and unload calls hold the HTTP response until readiness or terminal cleanup, avoiding an operation-job subsystem.
- **Return complete snapshots** — state and successful mutations report configured/active/pending models, one of six ordered lifecycle values, in-flight count, and all editable settings without commands, ports, PIDs, environment values, or config paths.

### Lifecycle and Policy Semantics
- **Share admission and lifecycle machinery** — manual loads use the same generation, bounded waiter, quick-switch, readiness, cancellation-safe ownership, and cleanup paths as inference loads. Manual controls do not create synthetic inference requests or rejection metrics.
- **Make safe repeats idempotent** — selecting the already-active healthy model does not restart it or reset its idle countdown. Unloading with no tracked model succeeds unchanged.
- **Reject unload overtaking** — a manual unload publishes shared stop-only ownership and returns `control_busy` instead of cancelling or overtaking a pending load/switch or another unload. Committed idle unload uses the same ownership marker.
- **Gate request-driven changes only** — `automatic_switch=false` rejects known inference requests that would load, join, or change a model while preserving explicit/model-less routing to the healthy active model. Manual loading bypasses this policy only; unknown-model, shutdown, stop-only busy, policy, and existing admission outcomes retain their defined precedence.
- **Preserve interruption behavior** — manual switches and unloads may terminate a process serving in-flight requests. Draining remains outside this change.

### Runtime Settings
- **Replace one exact three-field representation atomically** — automatic switching, quick switching, and idle timeout are validated together and assigned without an await. Idle-timeout changes cancel and restart an uncommitted idle countdown from update time; committed cleanup finishes.
- **Keep changes process-local** — restart restores automatic switching to enabled and restores quick-switch and idle-timeout values from the existing CLI inputs. Config/YAML, model definitions, commands, listener settings, and CLI contracts are unchanged.

### Trust Boundary
- **Accept unauthenticated local-network control on the existing listener** — this is the explicit deployment decision from the L4 interview. Every direct client that can reach the listener is trusted and can switch or unload configured models and change runtime settings.
- **Treat the custom header as CSRF resistance only** — mutations require `X-Model-Switcher-Control: 1`, PUTs require JSON, DELETE is bodyless, and no CORS permission is emitted. The header is not authentication or authorization because a direct client can forge it.
- **Keep authority constrained** — model commands and process details remain hidden and non-editable. The accepted trade-off is unsuitable outside a trusted local network; changed exposure requires a separate authentication design.

### Rejected Alternatives
- Bearer/session authentication, TLS, a second listener, persistence, asynchronous operation IDs, frontend dependencies, and model/config editing are excluded because they are unnecessary for the accepted local deployment and would create separate security or lifecycle systems.

### Status
- Implemented under P-20260923-01. The runtime API, control panel, shared lifecycle integration, persistent control-specific tests, and project documentation are complete; focused, related, and full-suite verification passed, followed by independent implementation and test audits.

## P-20260925-01 — Simplified Stats Dashboard

### Scope
- **Change the page only** — only the embedded `STATS_HTML` in `src/stats.py` changed. `StatsCollector`, `snapshot()`, and the `/stats/json` contract are unchanged, so JSON consumers are unaffected.

### Layout (revision 1; superseded by Revision 2 below)
- **Use one status line, one compact table, and one errors line** — the seven summary cards and two wide tables (23 columns) were replaced by a status line (loaded model, uptime, active/peak, swaps with average time in seconds, requests per minute), an 8-column table (Model, Reqs, Errors, Gen t/s, Best gen t/s, Prompt t/s, TTFT p50 / p95, Cache), and an errors line shown only when failures exist. *Superseded by Revision 2: the status line became six stat tiles and the errors line became an Errors card with chips.*
- **Put the loaded model first** — the loaded model's row is highlighted and listed first; other models are sorted by request count (ties by name). A final **All** row shows global totals; Best gen t/s is always `–` there because the global block has no `max_aggregate`. *Superseded by Revision 2: the **All** row became an **All models** totals row in `<tfoot>`; ordering is unchanged.*
- **Keep rendering safe and null-tolerant** — text-only DOM updates (no `innerHTML`), `–` for null, missing, or non-finite values, thousands separators for integers, 5-second refresh, and keep-last-render on fetch failure. No new dependencies.

### Dropped From the Page (Still in JSON)
- Request latency (avg/p50/p95/max), bytes in/out, raw token counts (prompt/completion/cached/prefill), process starts/stops, success rate, and `avg_rpm`. These are low-value at a glance or duplicate what the kept columns show; the user chose to keep speed, cache, response time (TTFT), and health/errors on the page.

### Revision 2 — Visual Polish
- **Same data, calmer presentation** — requested as "make it more aesthetically appealing". Metrics, table formatting, ordering, and the JSON contract are unchanged; only the `STATS_HTML` markup, CSS, and render code changed. Formatting changes are limited to the tiles: "req/min" is shown as "N in last min" under Requests, and large tile numbers are compacted (`1,234`, `12.3K`, `1.23M`) with the full number in the tooltip; table cells keep full numbers.
- **Palette via CSS custom properties** — page, surface, ink, hairline, baseline, accent, meter track, and status colors (good/serious/critical) are variables in `:root`, with dark values under `@media (prefers-color-scheme: dark)`. No categorical series colors are used: one accent plus a fixed status palette.
- **Stat tiles instead of a status line** — six tiles (Loaded model, Generation, Requests, Active now, Swaps, Uptime), each a label, a large value, and a small muted sub-line. Tiles use a fixed 6/3/2-column grid by width so they never leave a single orphan tile on its own row.
- **Status is dot + label, never color alone** — the header live indicator ("Connecting…", "Live · updated Ns ago", "Connection lost · showing last data" or "· retrying" before any data; a 200 response whose body is not a JSON object counts as a failure), the "Loaded" badge, error counts, and error chips all pair a colored dot with text.
- **Errors card instead of an errors line** — the rev-1 errors line became an **Errors** card with one chip (dot, kind, count) per error kind, shown only when failures exist.
- **Table stays the single detail view** — same 8 columns; the totals row is now "All models" in `<tfoot>` (separate border model so its top rule renders); Cache adds a small decorative meter (`aria-hidden`, width clamped to 0–100%, none when the rate is missing). Tabular figures are used only in the table.

### Status
- Implemented under P-20260925-01 with an executable Node-based page test (`tests/test_stats_page.py`).

## P-20260925-04 — Average Aggregate Speed and Token Totals

### Metric Definitions
- **Busy-time average, not wall-clock average** — `rates.generation_tps.avg_aggregate` is total cohort completion tokens divided by total cohort span, where a cohort is a connected group of overlapping eligible decode intervals (the same cohorts as `max_aggregate`). Only time with at least one eligible stream decoding counts; idle gaps between cohorts are excluded, so a quiet server does not look slow. `concurrency` is total per-stream decode time divided by the same busy time: the average number of streams decoding at once. A single stream gives concurrency 1.0 and the stream's own speed.
- **Same definition for prompt processing** — `rates.prompt_tps.avg_aggregate` uses one prefill interval per request, from proxy start to the first content chunk (`[first_token_at − ttft, first_token_at)`), carrying the uncached prompt tokens (`prompt − cached`, or `prompt` when cached is unknown). Eligibility follows `prompt_tps.avg` (successful streaming requests with a first token and at least one uncached prompt token) plus a positive prefill interval.
- **Global = sum of per-model totals** — the global block adds each model's tokens, busy time, and stream time. Busy time on different models is added, not merged into one timeline; this is fine because only one model is served at a time. (The plan's wording, "sums of per-model accumulators", is implemented literally.)
- **Shape** — `{"tps": number, "concurrency": number}` or `null` before any group closes; additive to the JSON contract, present per model and globally.

### Publication Rule
- **Publish only closed groups** — decode cohorts follow the existing `max_aggregate` rule (held while an active decode started before the cohort ended). A prefill group `[start, end)` is held while any in-flight request of that model started before `end` and has no first token yet or got its first token after `start`; such a request could still add an overlapping interval. Requests whose prefill ended before the group started, or that started at or after its end, do not hold it.
- **Hook** — the swapper records each proxied request's start (`record_request_start`) and closes it in the same `finally` block that closes the decode (`record_prefill_end`), sharing the request identity used for decode start/end. Every started request is released whether it succeeds, fails, or never produces a first token, so no pending state is left behind.
- **Accepted limitation** — while overlap continues (a never-idle server), nothing new is published until a gap occurs, the same as `max_aggregate`.

### Page
- **Columns** — Model, Reqs, Errors, Gen t/s, Gen total, Prompt t/s, Prompt total, TTFT p50 / p95, Cache, Gen tok, Prefill tok. The two "total" headers carry the tooltip "Combined speed while busy @ average concurrent requests". Gen total renders `85.3 @2.4` (one-decimal tps); Prompt total renders like Prompt t/s (whole number with thousands separators) plus one-decimal concurrency, e.g. `2,140 @1.8`; `–` when null or malformed. The All models row uses the global values.
- **Token totals return to the page** — Gen tok (`tokens.completion`) and Prefill tok (`tokens.prefill`) per model and in the All models row, shortened like the tiles (`45.2K`, `1.23M`) with the full number on hover. Reqs keeps full numbers.
- **Wider page** — the page max width grew from 64rem to 80rem so all 11 columns fit on a typical desktop (user choice); narrow screens still scroll the table sideways.
- **Best gen t/s dropped from the page, kept in JSON** — the column and its formatter were removed (user revision 2); per-model `max_aggregate` stays in `/stats/json` unchanged because the JSON contract is additive-only.

### Status
- Implemented under P-20260925-04 (rev 2) with collector unit tests, a real-stack JSON test, and Node page tests.

## P-20260926-01 — Live (Estimated) Generation Rate

### Approved Exception to "Exact Token Counts Only"
- **User-approved estimate** — the user chose to count streamed chunks and show an estimate marked "≈" (2026-09-26). This is a deliberate, limited exception to the exact-counts rule in P-20260913-02 above; every other token metric stays exact, and chunk counts never feed `tokens`, `last_60s`/`last_300s`, `avg`, or the aggregates.
- **Why** — exact `usage` arrives only when a stream ends, so exact windows jump at request end and show nothing while a long stream runs. Counting chunks is the only cheap in-flight signal.

### Definition
- **Unit** — one per choice in an OpenAI-style SSE `data:` event whose delta has non-empty `content`, `reasoning_content`, `reasoning`, or `tool_calls`, or (completions) non-empty `text`. Role-only, empty, usage-only, unparseable, and named non-OpenAI events count 0.
- **Rate** — superseded by § P-20260926-04 (busy-time denominator and tokens-per-chunk calibration). Originally `rates.generation_tps.live = {"tps": units in the last 60 s / 60, "window_s": 60, "estimate": true}` per model; global = sum of the per-model values; `tps` is `0` with no data.
- **Wiring** — `proxy.py` reports units through an optional `Outcome.gen_chunk_observer`, like `first_token_observer`, and never touches the collector. The swapper wires it to `StatsCollector.record_gen_chunks`, which keeps a per-model deque pruned to the live window (60 s originally, 15 s since § P-20260926-04). An observer failure is caught in the proxy so it cannot break a stream.

### Limits
- **OpenAI `data:` streams only** — FreeToken/Anthropic `/v1/messages` and `/v1/responses` named-event streams are not counted, and non-streaming requests never are.
- **One chunk is not always one token** — superseded by § P-20260926-04: chunks are now scaled by a per-model tokens-per-chunk ratio calibrated from finished streams. The ratio can still lag when chunk packing changes.
- **Lag** — superseded by § P-20260926-04: the rate now divides by busy time over a 15 s window and reads `0` when nothing is generating. The page still polls every 5 s.

### Page
- **"Live gen t/s ≈" tile and "Live t/s ≈" column** (after Gen t/s, per model and in the All models row), both with a tooltip (text updated by § P-20260926-04). Values use one decimal; `–` when the value is missing, non-numeric, or zero.
- **Seven tiles without orphans** — the Loaded model tile spans two columns, so the tile grid is 2/4/8 columns (breakpoints 40rem and 72rem) instead of the 6/3/2 grid from P-20260925-01.

### Status
- Implemented under P-20260926-01 with proxy chunk-count tests, collector window tests (fake clock), a mid-stream swapper integration test, and Node page tests.

## P-20260926-02 — Control Panel Matches the Stats Dashboard

### Scope
- **Page only** — only the embedded `CONTROL_HTML` markup, CSS, and render helpers in `src/control.py` changed. The control API, request headers, security rules, element IDs, and the 3 s refresh are unchanged. The 6 original control-panel tests pass unedited; one API test (`tests/test_control_api.py`) now checks the unchanged `<title>` instead of the old heading and still proves `/control` is served locally.

### Look
- **Copied, not shared, CSS** — `/control` copies the `/stats` colour variables (light plus a `prefers-color-scheme: dark` block), fonts, card, tile, dot, and pill styles, and the 80rem width. Sharing one stylesheet is deferred to the `src/stats.py` split (P-20260925-05); until then the two copies can drift, and a test checks that shared colour values match.
- **Header link** — title "Model Switcher" with the subtitle "Control panel · Statistics" linking to `/stats`, mirroring the `/stats` subtitle that links to `/control`.
- **Tiles and cards** — Lifecycle, Active model, Pending model, and In-flight requests are `/stats`-style tiles (2 columns, 4 from 40rem). Models and Runtime settings are cards with headings instead of fieldsets.
- **Status is dot + text** — the Lifecycle tile pairs a dot with a plain-language line (ready "Serving", loading/unloading/shutting down in orange, cleanup failed in red, idle grey); the loaded model's button has a green-bordered style plus a "Loaded" pill; errors show as a red-tinted message with a dot and the error text.
- **Themed controls** — shared button style, accent-coloured "Apply settings", accent checkboxes, and a matching number input. No live indicator (left out by the approved plan).

### Status
- Implemented under P-20260926-02 with static look tests and executable Node tests for the loaded-model marker and lifecycle dot; the 6 original control-panel tests are unedited, and one API check moved from the old heading to the unchanged `<title>`.

## P-20260926-04 — Live Generation Rate: Busy Time and Calibration

Supersedes the **Rate** line of § P-20260926-01. Units, wiring, limits of
chunk detection, and page layout from that section still apply.

### Problem
- **Slow to react** (Revision 2, user request 2026-09-26: "short the window") — the live window is now 15 s (`LIVE_WINDOW_S`), separate from the exact-token windows; `last_60s` / `last_300s` keep 60 s / 300 s.
- **Undercount** — "Live gen t/s ≈" divided chunks by a flat 60 s, so idle seconds counted as zero speed (15 s of generation at 400 t/s showed 100), and one chunk was counted as one token although vLLM/SGLang often pack several tokens per chunk under load. The value was already aggregate across streams.

### Definition
- **Busy time** — per model, the length of the union of decode intervals (first token → stream end) that ended in the last 15 s (any outcome) plus active decodes (`[first token, now]`), clipped to `[now − 15 s, now]`. An ended interval that straddles the window start counts only its part inside the window. Overlapping streams count once.
- **Tokens per chunk** — per model, Σ exact `completion_tokens` / Σ chunks over the last 50 finished successful streaming requests with `completion_tokens ≥ 1` and chunks ≥ 1; `1.0` with no samples. Failed, non-streaming, and usage-less requests add no sample. The proxy keeps a per-stream total in `Outcome.gen_chunks`; the swapper records the sample after a successful stream.
- **Rate** — per model: `tps = chunks in the last 15 s × tokens per chunk / max(busy time, 1 s)`; `0` while the model has no active decode (the page shows `–`). Global: Σ per-model calibrated tokens / max(union busy time across all models, 1 s) — a model finished before a swap and the new model's active decode both count, once each; `0` while no decode is active anywhere.
- **JSON** — `rates.generation_tps.live = {"tps", "window_s": 15, "estimate": true, "busy_s", "tokens_per_chunk"}`; `busy_s` is the unfloored busy time; `tokens_per_chunk` is per model and `null` in the global block. Additive to the P-20260926-01 shape.

### Limits
- **Average, not instant** — the value averages the last 15 s of busy time, so a speed change shows fully after about 15 s.
- **Still an estimate** — calibration uses past requests, so it lags when the chunk packing changes; a stream that is open but silent still counts as busy.
- **Exact metrics unchanged** — `tokens`, `last_60s`/`last_300s`, `avg`, and the aggregates never use chunks or the ratio. Observer and calibration input errors are ignored and cannot break a stream.

### Page
- **Tooltip** — the "Live gen t/s ≈" tile and "Live t/s ≈" column read "Estimated combined speed while generating, last 15 s (streamed chunks × tokens per chunk)"; the tile subtitle reads "estimate · 15 s". No layout change.

### Status
- Implemented under P-20260926-04 with fake-clock collector tests (busy time, union, clipping including an ended interval straddling the window start, union across a model swap, floor, calibration cap, `last_60s` still 60 s), proxy chunk-total tests, and swapper integration tests for calibration and failed/usage-less/non-streaming requests.

## P-20260927-01 — TabbyAPI Compatibility

### Backend Integration
- **Reuse the generic foreground-command contract** — TabbyAPI (theroyallab/tabbyAPI, ExLlamaV3) at master commit `816c321` accepts `--host`, `--port`, `--model-dir`, `--model-name`, and `--disable-auth` on the command line (overriding `config.yml`), opens its port only after the startup model has loaded, reports readiness on a public `GET /health`, serves OpenAI-compatible HTTP/SSE, and stops on SIGTERM. No backend adapter, dependency, registry, config field, alias, or request-body rewrite is needed.
- **Launch `main.py` from the TabbyAPI folder** — `config.yml`, `api_tokens.yml`, and `logs/` are relative to the working directory and commands run without a shell, so the recipe uses `bash -c "cd <tabbyAPI> && exec venv/bin/python main.py ..."`. `start.sh` / `start.py` may install packages or prompt, and `--config` drops the other command-line flags (including the assigned port), so neither is used. `TABBY_LOG_CONSOLE_WIDTH` stops 80-column wrapping when output is piped; multi-line messages such as tracebacks still emit indented continuation lines, logged as info.
- **Config key equals the model folder name** — TabbyAPI names the loaded model after the `--model-name` folder and ignores the request `model` while inline model loading is off (the default, which stays off). Model IDs are forwarded unchanged.

### Two Small Generic Changes
- **Recognize TabbyAPI log levels** — loguru/rich lines look like `HH:MM:SS.mmm LEVEL:    message` (time prefix optional with `--log-timestamps false`). A case-sensitive pattern for `TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL` followed by `:` runs after the NInfer pattern and before the generic one: WARNING maps to warning, ERROR/CRITICAL to error, the rest to info. The generic pattern did not accept the time prefix or know `SUCCESS`/`DEBUG`/`TRACE`; existing llama.cpp, vLLM, and NInfer classifications are unchanged.
- **Prefill progress is not the first token** — with `return_progress` (llama.cpp extension, also in TabbyAPI), prefill emits chunks with an empty delta or text and a top-level `prompt_progress`. Such an event, when it carries no generated output, no longer marks the first token, so time to first token and live busy time start at the first generated text. Usage on any event is still applied, and every other event keeps its existing first-token rule.

### Authentication
- **Keep auth off in the example** — user decision 2026-09-27: TabbyAPI has no flag or environment variable for a pre-set key (keys come only from `api_tokens.yml`), so the recipe uses `--disable-auth true`. Every caller that reaches the switcher then acts as a TabbyAPI admin; README states this risk.
- **Document the key-file option** — users who want auth drop the flag and write their own `api_key` / `admin_key` into `api_tokens.yml`. Clients send `x-api-key` or `Authorization: Bearer`, which the switcher already forwards unchanged; `/health` needs no key, so readiness is unaffected.

### Verification Trade-off
- **Use deterministic loopback fakes pinned to `816c321`** — tests shape readiness, `\r\n` SSE framing, `: ping` comments, the non-empty-choices usage chunk with timings, prefill progress chunks, log lines, and auth headers from the pinned sources and run the real config, health, switcher, process, and proxy paths without installing TabbyAPI, downloading models, or using a GPU. README documents a manual smoke check on real hardware.

## P-20260929-01 — Server-timed prompt speed

### Problem
- **Too low on ExLlamaV3** — user report 2026-09-29: TabbyAPI's own prompt speed was about twice ours. Prompt t/s divided `prompt − cached` by the proxy TTFT, which also holds tokenization and templating, queueing, the first decode step, and the SSE flush; TabbyAPI divides new tokens by GPU prefill time only.
- **Sky-high** — when the cached count was unknown (SGLang without `--enable-cache-report` or when nothing was cached, vLLM without `--enable-prompt-tokens-details`, llama.cpp cache only in `timings.cache_n`), the full prompt was counted as new work over a short TTFT.
- **Early first token** — any event with a non-empty `choices` list marked the first token, so a role-only opening chunk sent before prefill ended could shrink TTFT.

### Sources (pinned)
- **llama.cpp `1c47294`** — `usage.prompt_tokens_details.cached_tokens` always; top-level `timings` (`cache_n`, `prompt_n` = new tokens ≥ 1, `prompt_ms` = prefill only) on the non-stream body and the last stream chunk. No flags.
- **tabbyAPI `816c321` / exllamav3 v1.5.2** — `cached_tokens` always; `usage.prompt_time` (seconds, 10 ms rounding) and `prompt_tokens_per_sec`; llama.cpp-style `timings` with `prompt_n = prompt − cached` and `prompt_ms = prompt_time × 1000`.
- **vLLM `6a6c295`** — `cached_tokens` only with `--enable-prompt-tokens-details`; top-level `metrics.time_to_first_token_ms` (scheduled → first token) on the final usage chunk only with `--enable-per-request-metrics`.
- **SGLang `26d7539`** — `cached_tokens` only with `--enable-cache-report`, and omitted when 0; no prefill duration in the OpenAI response.

### Definition
- **Parsing** — the proxy reads `timings.{prompt_n, prompt_ms, cache_n}`, `usage.prompt_time`, and `metrics.time_to_first_token_ms` from the non-stream body and from every SSE event (last valid value wins, like usage). Only finite non-negative numbers are kept (integers for counts); bools and strings such as `"Indeterminate"` are ignored, and a parsing error never breaks proxying. Logic lives in `src/prefill.py`.
- **Cached count** — `usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`; this also feeds `tokens.cached`.
- **Sample priority** — per request: (a) `prompt_n ≥ 1` and `prompt_ms > 0` → `prompt_n` tokens over `prompt_ms`; (b) `usage.prompt_time > 0`; (c) `metrics.time_to_first_token_ms > 0`; (d) proxy TTFT (streaming only). (b)–(d) use `prompt − cached` tokens. Fewer than 1 token adds nothing. Only successful requests contribute.
- **Learn per model** — user decision 2026-09-29 (Q1): each model remembers whether it has ever reported a cached count (either source). For (b)–(d), a missing count means 0 once the model has reported; before that the request is left out of Prompt t/s, Prompt total, and Prefill tok and counted in `skipped_unknown_cache`. (a) already excludes the cache. The state lives in the per-model stats and is never reset while running.
- **Non-streaming** — user decision 2026-09-29 (Q2): included when (a)–(c) gives a server time. Aggregate interval: streaming `[max(start, first token − seconds), first token]`; non-streaming `[start, min(start + seconds, end)]`. The aggregate math is otherwise unchanged.
- **First token** — only an event with generated output (non-empty `delta.content` / `reasoning_content` / `reasoning` / `tool_calls`, or completions `text`) marks it; role-only, empty-delta, usage-only, and prefill-progress events do not. This is the same test that counts live-rate chunks.
- **JSON** — additive `rates.prompt_tps.server_timed_share` (server-timed ÷ all prefill tokens, `null` before any) and `rates.prompt_tps.skipped_unknown_cache` (count), per model and global. Other keys keep their meaning.

### Limits
- **Proxy TTFT is still the fallback** — SGLang reports no prefill time, and neither do vLLM without the metrics flag or llama.cpp/TabbyAPI when `prompt_ms` rounds to 0; those requests keep the TTFT-based number, which reads lower than the server's own.
- **Server clocks differ** — vLLM's TTFT starts at scheduling (excludes queueing), TabbyAPI rounds `prompt_time` to 10 ms, so very short prompts can fall through to the next source.
- **Proxy TTFT, TPOT, gen t/s, and the live rate** are unchanged except for the stricter first-token rule.

### Page
- **Tooltip** — the "Prompt t/s" column reads "Prompt processing speed; uses the server's own prefill time when available, else time to first token". No layout change.

### Status
- Implemented under P-20260929-01 with unit tests (`tests/test_prefill_timing.py`) and loopback-fake integration tests through the real proxy and switcher per backend (`tests/test_prefill_backends.py`). README lists the backend flags.

## P-20260929-03 — Server-timed generation speed

### Problem
- **Missing, not undercounted, per request** — user question 2026-09-29 ("exllamav3's tool call is non-streaming ... our token/s may undercount the non-streaming generations"). Decode time was measured only for streams (first output chunk → end), so non-streaming requests had tokens in the counters and `last_60s`/`last_300s` windows but no gen t/s, TPOT, or aggregate sample. A model used only without streaming showed no gen t/s.
- **Combined figures undercounted** — Gen total (`avg_aggregate`), `max_aggregate`, and the live rate dropped non-streaming tokens even though they shared the GPU with overlapping streams.
- **Buffered streams inflated** — a stream whose whole output arrives in one chunk (for example a TabbyAPI tool call) had a decode time of a few milliseconds, inflating gen t/s, Gen total, and `max_aggregate`, giving TPOT ≈ 0, and skewing the live-rate tokens-per-chunk calibration.

### Sources (pinned)
- **llama.cpp `1c47294`** — top-level `timings.predicted_n` / `predicted_ms` (first → last token) on the non-stream body and the last stream chunk. No flags. Its own `predicted_per_second` divides by the same N.
- **tabbyAPI `816c321` / exllamav3 v1.5.2** — `usage.completion_time` (seconds, 10 ms rounding) and llama.cpp-style `timings.predicted_n` / `predicted_ms`. With tools and `stream: true` it is one generation pass: text before the tool tag streams, the tool-call text is held back and emitted in the final (finish) chunk; `usage`/`timings` include the tool-call tokens and time. A reply that opens with a tool call sends nothing until the end.
- **vLLM `6a6c295`** — top-level `metrics.generation_time_ms` (first → last token) on the final usage chunk and the non-stream body, only with `--enable-per-request-metrics` (n = 1).
- **SGLang `26d7539`** — no decode duration in the OpenAI response for streaming; `meta_info` only on non-streaming requests with `return_meta_info`, which the switcher does not set.
- **Tool parsers** — vLLM, SGLang, and llama.cpp tool parsers can also hold back text (a gap, then a burst); the effect is parser-dependent.

### Definition
- **Parsing** — the proxy reads `timings.predicted_n` + `predicted_ms` (accepted only as a valid pair from one event), `usage.completion_time`, and `metrics.generation_time_ms` from the non-stream body and every SSE event; last valid value wins. `timings.prompt_n` + `prompt_ms` are now also taken only as a pair (review note on P-20260929-01), and integers too large for a float are ignored without skipping the event's other fields. Logic lives in `src/decode.py`; parsing errors never break proxying.
- **Sample priority** — per successful request with `completion_tokens ≥ 1`: (a) `predicted_n ≥ 1` and `predicted_ms > 0` → `predicted_n` tokens over `predicted_ms`; (b) `usage.completion_time > 0`; (c) `metrics.generation_time_ms > 0`; (d) the proxy decode time (streaming only), used only when the stream had ≥ 2 generated-output chunks. (b)–(d) use `completion_tokens`. A stream that fails the (d) chunk rule is left out and counted once in `skipped_buffered` (per model and global); a non-streaming request without server time is simply not timed.
- **Convention** — speed is tokens ÷ time (N, not N − 1), matching TabbyAPI and the existing proxy measure; llama.cpp's and vLLM's times span first → last token (N − 1 decode steps), so N ÷ time reads slightly higher than the per-step rate (N − 1) ÷ time; llama.cpp's own `predicted_per_second` also uses N, so it matches ours. TPOT stays `seconds / (tokens − 1)`.
- **Uses** — the sample feeds `rates.generation_tps.avg`, `tpot_ms`, `avg_aggregate`, and `max_aggregate`, streaming or not.
- **Aggregate interval** — server-timed `[max(request start, end − seconds), end]`; proxy-timed `[first chunk, end]` (unchanged). Because a server-timed interval can start anywhere after its request started, a decode group is now published only when no in-flight request of that model (streaming or not, other than the one ending) started before the group ended; before, only active stream decodes held a group. Failed or cancelled requests add no interval, so they still only delay publication and never bridge groups.
- **Live rate** — streams with fewer than 2 output chunks give no tokens-per-chunk calibration sample. Busy time and chunk counting are unchanged; non-streaming requests still do not count.
- **JSON** — additive `rates.generation_tps.server_timed_share` (server-timed ÷ all decode tokens, `null` before any) and `rates.generation_tps.skipped_buffered` (count), per model and global. `last_60s`/`last_300s` are unchanged.

### Limits
- **SGLang** — streaming keeps the proxy timing (with the ≥ 2 chunks rule); non-streaming generation is not timed.
- **vLLM without `--enable-per-request-metrics`** and llama.cpp/TabbyAPI replies whose times round to 0 fall through to the next source, as for prompt speed.
- **Two-chunk bursts** — a held-back tool call streamed in two or more chunks at the end still counts with the proxy time when the server gives no decode time.
- **Proxy TTFT and prompt t/s** are unchanged; a buffered stream's TTFT-based prompt fallback still includes decode time.
- **Held publication** — a long queued, prefilling, or non-streaming request that started before a decode group ended holds that group's "Gen total" (`avg_aggregate`) and `max_aggregate` publication until the request ends (at most the proxy timeout); the published numbers are unchanged.

### Page
- **Tooltip** — the "Gen t/s" column reads "Generation speed; uses the server's own decode time when available, else first to last streamed chunk". No layout change.

### Status
- Implemented under P-20260929-03 with unit tests (`tests/test_decode_timing.py`) and loopback-fake integration tests through the real proxy and switcher per backend (`tests/test_decode_backends.py`). README lists the backend flags. Earlier sections that say non-stream timing is unobservable are kept as history.
