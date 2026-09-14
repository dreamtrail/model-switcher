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
- **"tokens/second" is always two rates** — prompt-processing t/s (prefill) and generation t/s (decode), token-weighted aggregates (Σtokens/Σtime), never blended.
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
