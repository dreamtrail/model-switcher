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
- **Upstream logs keep model prefixes** — captured upstream output is emitted as `model-name: message` so multiple models remain distinguishable.
- **Stderr does not imply warning** — upstream stdout/stderr lines default to INFO unless common severity markers indicate WARNING or ERROR.

### Streaming
- **Chunked SSE passthrough** — only correct choice for chat completions. Read/write chunks in real-time via aiohttp async streaming.

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
