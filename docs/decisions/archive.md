# Decisions Archive

Complete decision records before P-20260925, preserved as history rather than a statement of current behavior.

Return to the [decision index](../DECISIONS.md), or read the [recent statistics and dashboard records](stats.md).

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
- **Outcome classification**: success = final status < 400 with no mid-stream error; errors keyed by kind (`400`/`404`/`502`/`503`/`504`/`stream_error`/`client_disconnect`/`error`); swap = any stop-then-start in `_ensure_model` incl. same-model restarts and crash recovery, duration only on successful health-readiness.

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

Return to the [decision index](../DECISIONS.md).
