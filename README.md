# Model Switcher

Automatic model swapping reverse proxy for local AI servers. Point your client at the switcher, and it handles starting, stopping, and routing to the correct model — no manual intervention needed.

## Quick Start

```bash
# Install dependencies
python -m venv .venv
.venv/bin/pip install -r requirements.txt

# Copy and edit the config
cp config.example.yaml config.yaml
# edit config.yaml with your model paths

# Run
./run.sh --config config.yaml --host 0.0.0.0 --port 8080
```

To run it as a background systemd service instead, see [docs/SETUP.md](docs/SETUP.md) — including the **linger** requirement that keeps user units alive across SSH disconnects.

Point your client at `http://localhost:8080` and use model names from your config:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama-3.1-8b",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Configuration

YAML file mapping model names to commands:

```yaml
llama-3.1-8b: "llama-server --model /path/to/model.gguf --port ${PORT} --ctx-size 8192"
mistral-7b: "llama-server --model /path/to/mistral-7b.gguf --port ${PORT}"
```

- `${PORT}` — replaced at runtime with a free port.
- Commands run in the foreground; the switcher manages their lifecycle.

### Optional shutdown hooks

String entries and mappings with `cmd` remain supported. A mapping can also
specify a user-owned local shutdown command:

```yaml
my-model:
  cmd: "my-model-server --port ${PORT}"
  shutdown_cmd: "/path/to/stop-my-model --model my-model"
  shutdown_timeout: 30
```

`shutdown_cmd` must be a nonempty, parseable command. `shutdown_timeout` defaults
to 30 seconds and must be a positive finite number (not a boolean); specifying
it without a hook is rejected. Hooks run locally by direct execution, with no
implicit shell, inherited environment and working directory, and disconnected
stdin. Arguments are literal: no `${PORT}`, environment expansion, or startup
port rewriting applies. Hook stdout/stderr are discarded; hook commands and
script output are not included in API errors or hook logs.

The hook runs **before** local process-group termination on switches (including
supersession), manual/idle unload, post-spawn startup failures or cancellation,
and graceful shutdown—even if the launcher has already exited. A failed exec
before a process handle exists does not run a hook. Local termination is always
attempted, even when the hook fails. A replacement starts only after the hook
exits zero and local cleanup is confirmed. Missing executables, nonzero exit,
timeout, or unresolved process groups fail closed as `process_cleanup_failed`.
Later lifecycle attempts retry only unfinished stages; there is no automatic
retry loop. Successful stages and stop statistics are not repeated.

Scripts must be model-scoped, idempotent, noninteractive, and wait until their
workers have actually stopped; already stopped must count as success. Exit zero
is the script's confirmation, **not independent verification of remote state**.
No remote scripts or SSH integration are included. See
[service stop budgets and limitations](docs/SETUP.md#shutdown-hook-stop-budgets).

## Quick Switching

By default, a request for the model that is already loading joins that load. A request for a different model is refused immediately with HTTP 409 and `error.code: "switch_in_progress"`; it is not queued to trigger another slow load afterward.

Enable quick switching when a newer different-model request should replace the pending target:

```bash
./run.sh --config config.yaml --quick-switch
```

With `--quick-switch`, the latest admitted model wins. Requests for the same target still join one load, while requests made obsolete by a newer target receive HTTP 409 with `error.code: "swap_superseded"`. Supersession interrupts readiness polling only: the obsolete model's process group is stopped before its replacement starts. If the requested model had already been stopped, switching back loads it again.

Process creation failures return HTTP 500 with `error.code: "model_start_failed"`. If an optional shutdown hook fails, or SIGTERM, the existing grace period, and SIGKILL cannot confirm that the old process group exited, switching fails closed with HTTP 503 and `error.code: "process_cleanup_failed"`; no replacement starts until cleanup is confirmed. Graceful cleanup can therefore delay a quick switch by several seconds, but prevents overlapping model servers and GPU-memory contention.

## Control Panel and Runtime API

Open `http://localhost:8080/control` to use the dependency-free control panel on the existing listener. It shows configured, active, and pending models, lifecycle state, in-flight request count, and the three runtime settings. It can load or switch to a model, unload the active model, and update settings. It never displays or edits model commands, assigned ports, process IDs, environment values, or the config path.

> **Network trust warning:** the control API has no authentication or authorization. `X-Model-Switcher-Control: 1` protects browsers against cross-site request forgery; it does **not** prove who the caller is. Binding model-switcher to `0.0.0.0` or another shared interface grants every network-reachable client process-control authority. Use this only on a trusted local network. If the listener is exposed beyond that boundary, put an authenticated service in front of it or do not expose it.

The switcher-local routes are:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/control` | Browser control panel |
| `GET` | `/api/v1/control/state` | Full runtime state |
| `PUT` | `/api/v1/control/settings` | Replace all three runtime settings |
| `PUT` | `/api/v1/control/model` | Load or switch to one configured model |
| `DELETE` | `/api/v1/control/model` | Unload the tracked model |

`/control` and every path at or below `/api/v1/control` are reserved and intentionally shadow matching upstream routes. State responses use `Cache-Control: no-store`. Every mutation requires `X-Model-Switcher-Control: 1`; PUT requests also require `Content-Type: application/json` and a body no larger than 4 KiB. DELETE is bodyless. The service emits no CORS permission headers.

Example state:

```json
{
  "models": ["llama-3.1-8b", "mistral-7b"],
  "active_model": "llama-3.1-8b",
  "pending_model": null,
  "lifecycle": "ready",
  "inflight_requests": 0,
  "settings": {
    "automatic_switch": true,
    "quick_switch": false,
    "idle_timeout_seconds": 1800.0
  }
}
```

The lifecycle vocabulary is `idle`, `loading`, `ready`, `unloading`, `cleanup_failed`, and `shutting_down`. Mutations wait for model readiness or confirmed cleanup and then return the full state document. Loading the already-active healthy model and unloading when no model is tracked are successful no-ops.

Settings updates are complete replacements:

```bash
curl -X PUT http://127.0.0.1:8080/api/v1/control/settings \
  -H 'Content-Type: application/json' \
  -H 'X-Model-Switcher-Control: 1' \
  -d '{"automatic_switch":false,"quick_switch":true,"idle_timeout_seconds":600}'

curl -X PUT http://127.0.0.1:8080/api/v1/control/model \
  -H 'Content-Type: application/json' \
  -H 'X-Model-Switcher-Control: 1' \
  -d '{"model":"mistral-7b"}'

curl -X DELETE http://127.0.0.1:8080/api/v1/control/model \
  -H 'X-Model-Switcher-Control: 1'
```

`automatic_switch=false` pins inference routing to the currently healthy model. Explicit requests for that model and model-less requests routed to it continue; known requests that would load, join, or change a model return HTTP 409 with `error.code: "automatic_switch_disabled"`. Unknown models remain 404. Manual model controls bypass only this automatic-switch policy: they still share the same bounded admission, latest-wins quick-switch setting, cleanup, readiness, and shutdown rules. A manual unload returns 409 `control_busy` while a load/switch or another unload is pending. Disabling automatic switching does not cancel work already admitted.

Changing a positive idle timeout restarts the countdown from the update time when a healthy model is idle; zero disables idle unload. Cleanup already in progress still finishes. Manual switches and unloads preserve the existing model-change behavior and may interrupt in-flight requests using the previous process; the panel shows the in-flight count but does not drain requests.

All control settings are process-local. Restart restores `automatic_switch=true`, the command-line `--quick-switch` value, and the command-line `--idle-timeout` value; no YAML is changed.

## SGLang

[SGLang](https://docs.sglang.io/) works through the generic foreground-process interface. Follow its official [quickstart and installation guidance](https://docs.sglang.io/docs/get-started/quickstart) for your platform and accelerator, and confirm that the selected model fits the available device memory. The supported integration described here targets the OpenAI-compatible API behavior in [SGLang v0.5.19](https://github.com/sgl-project/sglang/releases/tag/v0.5.19).

Use `sglang serve`, bind it to loopback, and make the config key, `--served-model-name`, and API request `model` identical:

```yaml
qwen2.5-7b-sglang: "sglang serve --model-path /path/to/Qwen2.5-7B-Instruct --served-model-name qwen2.5-7b-sglang --host 127.0.0.1 --port ${PORT}"
```

Model-switcher executes commands directly rather than through a shell. The `sglang` executable must therefore be available in the environment that runs model-switcher; shell activation, pipes, and inline environment assignments are not interpreted.

During normal SGLang startup, `/health` returns 503 while model warm-up continues and 200 only when the server is ready, even though `/v1/models` may already return 200. Model-switcher treats an implemented `/health` endpoint as authoritative and uses `/v1/models` as a readiness fallback only when `/health` returns 404 or 405.

OpenAI-compatible Chat Completions JSON and SSE streams are forwarded unchanged, including the terminal `data: [DONE]`. SGLang accepts the switcher's automatic `stream_options: {"include_usage": true}` addition for streamed Chat Completions. If the selected SGLang version and model expose `POST /v1/responses`, its named SSE events are also passed through unchanged; that endpoint is optional in SGLang and is not guaranteed by a successful health check. SGLang-native endpoints such as `/generate` are not covered by this integration.

For a hardware-backed smoke check, start model-switcher with this config. Models start lazily, so in another terminal first send a model-selecting request; it will wait while SGLang starts:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-7b-sglang","messages":[{"role":"user","content":"Hello"}]}'
```

Once the startup log shows SGLang's assigned port, inspect readiness and the advertised model ID. After `/health` returns 200 and the trigger request completes, repeat the non-streaming request above and then check streaming:

```bash
curl -i http://127.0.0.1:<assigned-port>/health
curl http://127.0.0.1:<assigned-port>/v1/models
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen2.5-7b-sglang","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

On a swap or shutdown, model-switcher sends SIGTERM to SGLang's process group, waits for the existing grace period, and uses its forced-stop fallback if needed.

## FreeToken

[FreeToken](https://github.com/FlashML-org/FreeToken) works through the generic foreground-process interface. This integration targets [FreeToken v0.1.3](https://github.com/FlashML-org/FreeToken/releases/tag/v0.1.3), at release commit `cac247a860e316e06580d05aeb05f2e647bde214`. Follow that release's [installation guidance](https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/docs/install.md): the v0.1.3 CLI requires Linux x86_64, Python 3.10 or newer, an NVIDIA GPU with driver r580 or newer, and a CUDA 13 toolkit with `nvcc` for first-use kernel compilation. Install FreeToken and choose compatible model weights that fit the available GPU and host memory; it is not a model-switcher dependency.

Launch `ft serve` directly in the foreground, bind it to loopback, and make the config key, `--served-model-name`, and API request `model` identical:

```yaml
freetoken-model: "ft serve --model /path/to/model --served-model-name freetoken-model --host 127.0.0.1 --port ${PORT}"
```

Model-switcher executes commands directly rather than through a shell, so the `ft` executable must be available in the environment that runs model-switcher. Shell activation, pipes, and inline environment assignments are not interpreted.

FreeToken consumes both the selected API port and the adjacent port for its internal distributed address. Automatic selection therefore probes the base and base + 1 simultaneously and never selects 65535. The probes close before lazy startup, preserving the switcher's existing best-effort port-selection race; another process can still claim either port before `ft serve` starts.

FreeToken's `/health` returns HTTP 200 throughout its `loading`, `ok`, and `error` lifecycle. Model-switcher waits until the JSON status is exactly `ok`; `loading` and `error` remain pending and never trigger `/v1/models` fallback. A persistent error follows the normal readiness timeout and process cleanup path. Generic backends retain the existing behavior for other HTTP 200 bodies, and `/v1/models` fallback remains limited to `/health` returning 404 or 405.

The v0.1.3 compatibility boundary is representative transparent transport, not exhaustive OpenAI, Anthropic, or Responses API conformance:

| Route | JSON certification | Streaming certification |
|---|---|---|
| `/v1/chat/completions` | OpenAI-shaped JSON passes through unchanged | `data:` JSON passes through; model-switcher injects `stream_options.include_usage` when absent; a usage chunk and terminal `data: [DONE]` are preserved |
| `/v1/completions` | OpenAI-shaped JSON passes through unchanged | `data:` JSON passes through; model-switcher injects `stream_options.include_usage` when absent; a usage chunk and terminal `data: [DONE]` are preserved |
| `/v1/messages` | Anthropic-shaped JSON passes through unchanged | Named Anthropic SSE events pass through `message_stop`, with no `[DONE]` |
| `/v1/responses` | Responses-shaped JSON passes through unchanged | Named Responses SSE events pass through representative `response.completed`, with no `[DONE]` |

Upstream status codes, bodies, and headers are forwarded without FreeToken-specific wire transformations. The matrix certifies these representative JSON and stream shapes only; it does not certify every optional request parameter or upstream feature. In v0.1.3, for example, OpenAI `n > 1`, `logit_bias`, and constrained JSON response formats are rejected, and the Responses API is a stateless subset: background mode and `previous_response_id` are unsupported, while stored-response retrieval and cancellation are not available. Check the pinned [CLI](https://github.com/FlashML-org/FreeToken/blob/cac247a860e316e06580d05aeb05f2e647bde214/docs/cli.md) and API sources for the complete upstream boundary.

On a swap or shutdown, model-switcher terminates the foreground FreeToken process group using its normal SIGTERM grace period and forced-stop fallback. It does not manage FreeToken's separate shell, desktop, control, cache, or stats workflows.

A real-hardware smoke check is optional and non-gating. On a supported host, install v0.1.3, verify `ft --version`, configure the recipe above, and start model-switcher. The first request starts FreeToken lazily and can take longer while model weights load and CUDA kernels compile:

```bash
ft --version
./run.sh --config config.yaml --host 127.0.0.1 --port 8080

curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"freetoken-model","messages":[{"role":"user","content":"Reply with hello"}]}'
```

After startup, use the assigned port shown in model-switcher's log to confirm the lifecycle status and served model, then exercise each stream terminal convention through the switcher:

```bash
curl http://127.0.0.1:<assigned-port>/health
curl http://127.0.0.1:<assigned-port>/v1/models

curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"freetoken-model","messages":[{"role":"user","content":"Reply with hello"}],"stream":true}'
curl -N http://127.0.0.1:8080/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"freetoken-model","prompt":"Reply with hello","stream":true}'
curl -N http://127.0.0.1:8080/v1/messages \
  -H 'Content-Type: application/json' -H 'anthropic-version: 2023-06-01' \
  -d '{"model":"freetoken-model","max_tokens":32,"messages":[{"role":"user","content":"Reply with hello"}],"stream":true}'
curl -N http://127.0.0.1:8080/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model":"freetoken-model","input":"Reply with hello","stream":true}'
```

Confirm the two OpenAI-style streams end in `data: [DONE]`, the Messages stream reaches `message_stop` without `[DONE]`, and the Responses stream reaches `response.completed` without `[DONE]`. This manual procedure validates the local FreeToken/CUDA/model combination; automated compatibility checks intentionally require no FreeToken installation, model download, or GPU.

## NInfer

[NInfer](https://github.com/Neroued/ninfer) works through the same foreground-process interface. At the time this support was added, NInfer required 64-bit Linux, an RTX 5090 (SM120a), CUDA Toolkit 13.1 or newer, and a supported `.ninfer` artifact; check its upstream documentation for current requirements and build instructions.

Use the artifact's exact embedded model ID as the config key. Do not pass a `--model-id` override:

```yaml
qwen3.8-27b: "/path/to/ninfer/build/apps/ninfer-serve /path/to/models/qwen3_8_27b_nvfp4.ninfer --host 127.0.0.1 --port ${PORT}"
```

Current NInfer artifacts expose these embedded IDs:

- `qwen3.6-27b`
- `qwen3.8-27b`
- `qwen3.6-35b-a3b`

The API request's `model` must match the config key exactly, including case. The groupwise and NVFP4 artifacts for each 27B family share the same embedded ID, so both weight variants of that family cannot be configured simultaneously without aliases. Model-switcher does not rewrite model IDs.

NInfer's `/health` becomes available after model loading and warm-up. Model-switcher then proxies its OpenAI-compatible JSON and SSE output unchanged: Chat Completions streams end with `data: [DONE]`, while Responses API streams use named `event:` records and a terminal response event without `[DONE]`. Upstream statuses, error bodies, and headers such as `x-request-id` are forwarded normally. On a swap or shutdown, model-switcher sends SIGTERM before its existing forced-stop fallback.

## Statistics

Model-switcher tracks runtime statistics in memory (reset on restart) and serves them at two switcher-local endpoints:

- `GET /stats` — human-readable dashboard that refreshes every 5 seconds and follows the system light/dark theme. The header links to the control panel (`/control`) and shows a live indicator: "Connecting…" before the first response, "Live · updated Ns ago" after each good response, and "Connection lost · showing last data" when a refresh fails (the last good data stays on screen), or "Connection lost · retrying" if no data has arrived yet. Six stat tiles show the loaded model, average generation speed (tok/s), requests (with errors and requests in the last minute), active requests (with peak), swaps (with average swap time), and uptime; large tile numbers are shortened (for example `12.3K`, `1.23M`) with the full number on hover. A **Models** card holds one compact table with a row per model plus an **All models** totals row (Reqs, Errors, Gen t/s, Gen total, Prompt t/s, Prompt total, TTFT p50 / p95, Cache hit % with a small bar, Gen tok, Prefill tok). **Gen total** and **Prompt total** show the combined speed while busy and the average number of requests running at once, for example `85.3 @2.4` (see `avg_aggregate` below); **Gen tok** and **Prefill tok** are the generated and uncached prompt token totals, shortened like the tiles with the full number on hover. The loaded model is listed first with a "Loaded" badge, and other models are sorted by request count. An **Errors** card with one chip per error kind appears only when failures exist. The page intentionally shows a subset of the metrics; the full data (latency, bytes, prompt/cached token counts, starts/stops, windows, the per-model best cohort `max_aggregate`) is in `/stats/json`.
- `GET /stats/json` — the same data as JSON:

```json
{
  "uptime_seconds": 3600.5,
  "current_model": "llama-8b",
  "concurrency": {"active": 1, "peak": 3},
  "requests": {
    "total": 42, "success": 40, "failure": 2, "success_rate": 0.952,
    "errors": {"502": 1, "stream_error": 1},
    "latency_ms": {"count": 42, "avg": 120.5, "p50": 100.0, "p95": 300.0, "max": 320.0},
    "bytes_in": 1024, "bytes_out": 40960,
    "tokens": {"prompt": 8100, "completion": 2300, "cached": 512, "prefill": 7588, "cache_hit_rate": 0.063},
    "rates": {
      "prompt_tps": {"avg": 890.2, "avg_aggregate": {"tps": 1240.6, "concurrency": 1.4}},
      "generation_tps": {
        "avg": 38.1, "last_60s": 41.5, "last_300s": 39.0,
        "avg_aggregate": {"tps": 85.3, "concurrency": 2.4}
      }
    },
    "ttft_ms": {"count": 40, "avg": 210.0, "p50": 180.0, "p95": 640.0, "max": 700.0},
    "tpot_ms": {"count": 40, "avg": 26.3, "p50": 25.0, "p95": 30.0, "max": 31.0}
  },
  "throughput": {"last_60s": 5, "last_300s": 12, "avg_rpm": 0.7},
  "swaps": {"count": 2, "avg_ms": 1500.0, "p95_ms": 2100.0, "max_ms": 2100.0},
  "models": {
    "llama-8b": {
      "starts": 1,
      "stops": 0,
      "requests": {
        "rates": {
          "generation_tps": {
            "avg": 38.1,
            "last_60s": 41.5,
            "last_300s": 39.0,
            "avg_aggregate": {"tps": 85.3, "concurrency": 2.4},
            "max_aggregate": {"tps": 107.8, "concurrency": 3}
          }
        }
      }
    }
  }
}
```

Metric semantics:

- Counted requests are proxied requests plus rejected ones; `GET /v1/models`, `GET /models`, and the stats endpoints themselves are excluded.
- Success means the final response status is below 400 with no mid-stream error. Failures are keyed by kind: rejection kinds (`"400"`, `"404"`, `"503"`, `"504"`), upstream statuses (`"500"`, `"502"`, ...), mid-stream SSE errors (`"stream_error"`), and exceptions (`"error"`). Null fields (`success_rate`, `latency_ms`, swap durations) mean "no data yet".
- Latency is the upstream round-trip only: it excludes swap-queue wait and model-load time, so a request that triggers a cold start or swap reports its post-load latency.
- Swaps count every stop-then-start in the model manager, including same-model restarts and crash recovery; swap durations are recorded only when the swap completes with a healthy model.

Token metrics (when the upstream reports an OpenAI `usage` field):

- Token counters are exact counts read from `usage`: `prompt`, `completion`, and `cached` (from `prompt_tokens_details.cached_tokens` when present). Counters are recorded whenever usage is available, regardless of request status; there is no estimation — when usage is absent, the fields show no data.
- `prefill` is the uncached prompt work — `prompt − cached` when the backend reports `cached`, the full `prompt` otherwise — summed only for requests where time-to-first-token was observed.
- `cache_hit_rate` is the token-weighted lifetime ratio `cached ÷ prompt` (per model and global); it is `null` when no prompt tokens were recorded. Like the counters, it is derived from exact reported counts — no estimation. Backends that report cumulative cached counts may yield a ratio above 1; the value is reported as-is.
- For streaming requests the switcher transparently adds `"stream_options": {"include_usage": true}` to chat/completions requests (`/v1/chat/completions`, `/v1/completions`) whose clients did not set `stream_options`, so token metrics are available even when the client doesn't ask for them. Client-set values are forwarded untouched; non-JSON and non-streaming bodies pass through unchanged.
- Timing metrics are streaming-only, since only streamed responses reveal token boundaries:
  - `ttft_ms` (time to first token) — from proxy start to the first content chunk; captures queueing, prompt processing, and model behavior. Percentiles, not averages.
  - `tpot_ms` (time per output token) — decode-window time divided by `(completion_tokens - 1)`; requires at least 2 completion tokens.
  - `rates.generation_tps` — completion tokens per second of decode time (token-weighted), with trailing `last_60s`/`last_300s` windows; `rates.prompt_tps` — uncached prompt tokens (`prompt − cached` when the backend reports it; full `prompt` otherwise) per second of time-to-first-token. The two rates are never blended.
  - Per-model `rates.generation_tps.max_aggregate` is separate from those average and rolling rates. It retains the highest completed decode cohort as `{"tps": number, "concurrency": integer}` until restart; it is not added to the global generation-rate object.
  - A cohort is a maximal connected group of positively overlapping decode intervals, from each request's first token through its final stream end. Its aggregate rate is the sum of exact completion tokens divided by the cohort's wall-clock span; concurrency is the peak number of simultaneously decoding eligible streams. Merely touching intervals are separate.
  - Only successful streaming requests with exact usage reporting at least one completion token and a positive decode duration contribute. A continuously overlapping cohort remains unpublished until it closes. The dashboard no longer shows this value (P-20260925-04); it stays in the JSON.
  - `rates.generation_tps.avg_aggregate` (per model and global) is the busy-time average over all completed cohorts: `tps` = total cohort tokens ÷ total cohort span, and `concurrency` = total per-stream decode time ÷ total cohort span (the average number of streams decoding at once). Only time with at least one eligible stream decoding counts, so idle gaps do not lower it. One stream alone gives `concurrency` 1.0 and the same `tps` as that stream. It is `null` until a cohort closes. The global value sums the per-model totals: busy time on different models is added, not merged into one timeline, which is fine because only one model is served at a time.
  - `rates.prompt_tps.avg_aggregate` is the same busy-time average for prompt processing. Each request's prefill interval runs from proxy start to its first content chunk (its TTFT) and carries its uncached prompt tokens (eligibility follows `prompt_tps.avg` — successful streaming requests with a first token and at least one uncached prompt token — plus a positive prefill interval). The global value sums the per-model totals in the same way. Overlapping intervals form groups; a group is published only when no in-flight request of that model could still overlap it (one that started before the group ended and has no first token yet, or whose first token came after the group started). Requests that fail or never produce a first token are released and never hold a group once they end.
  - The dashboard renders both as `<tps> @<concurrency>` with one-decimal concurrency — **Gen total** with one-decimal tps (for example `85.3 @2.4`) and **Prompt total** as a whole number like **Prompt t/s** (for example `2,140 @1.8`) — and `–` before any group is published. Like `max_aggregate`, a server that is never idle publishes nothing until a gap occurs.
- Non-streaming requests contribute token counts only; no timing split is observable from a single whole-body response.

Known limitations: these paths shadow upstreams serving the same paths (like `/v1/models`); latency samples are unbounded in memory (fine for a local dev tool); requests naming unknown models each create a per-model stats row keyed by the client-supplied name; the pending SSE line buffer is capped (~64 KiB) — once the cap is hit, non-line-shaped content (e.g. a binary audio stream without newlines) is discarded, so token metrics may be absent for such streams (byte counts remain exact).

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to YAML config |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Listen port |
| `--timeout` | `60` | For positive values: total swap/non-stream timeout and stream connection/read-inactivity timeout (seconds); streams have no total lifetime limit |
| `--idle-timeout` | `30m` | Unload idle model after duration (`900`, `900s`, `15m`, `1h`; `0` disables) |
| `--quick-switch` | off | Let a newer different-model request supersede a model that is still loading |
| `--timestamp` | off | Include timestamps in log output |
| `--version` | — | Print version |

For positive `--timeout` values, model swaps and non-streaming requests must finish within the configured total duration. Streaming requests may continue for any total duration while data keeps arriving; establishing the upstream connection or waiting between chunks for longer than the configured duration ends the stream through the existing error path.

## API Endpoints

- `GET /v1/models` — list available models (static from config)
- `GET /models` — alias for `/v1/models`
- `* /{path:.*}` — proxied to the active upstream model

## How It Works

1. Client sends a request with a `model` field.
2. Switcher checks if that model is running.
3. If not (or a different model is active), it stops the current one and starts the requested model.
4. It waits for the new model to report healthy (`/health` endpoint).
5. The request is proxied to the upstream server.
6. Streaming responses (SSE) are passed through in real-time.

Only one model runs at a time.

## Idle VRAM Release

By default, the switcher unloads the active model after **30 minutes** without proxied model traffic, releasing VRAM while keeping the switcher running. Configure this with `--idle-timeout`:

```bash
./run.sh --config config.yaml --idle-timeout 30m
./run.sh --config config.yaml --idle-timeout 0    # disable idle unload
```

Accepted durations are bare seconds or integer `s`/`m`/`h` suffixes, such as `900`, `900s`, `15m`, or `1h`. The idle timer starts after the last in-flight upstream request finishes; local model-list endpoints (`GET /v1/models`, `GET /models`) do not keep a model loaded.

## Output

Log output is color-coded in the terminal for readability:
- **Green** — INFO events (startup, health ready)
- **Yellow** — WARNING events
- **Red** — ERROR events
- **Cyan** — Model swap/start events
- **Magenta** — Model stop (SIGTERM)

Upstream model logs are shown as the server emitted them, without repeated model-name prefixes or Python logger names such as `src.process_manager`. Timestamps are hidden by default; enable them with `--timestamp`. Normal upstream output omits the switcher's `[INFO]` label and color; warning/error markers from upstream remain formatted as `[WARNING]` or `[ERROR]`. Output written to stderr is not automatically treated as a warning; severity is inferred from common upstream markers when possible, and otherwise uses the normal upstream format.

NInfer's `[YYYY-MM-DD HH:MM:SS.mmm] [info|warning|error] ninfer-serve: ...` lines retain their complete upstream text. Their outer bracketed level controls switcher formatting, so an NInfer request line marked `[info]` remains normal output even if its message contains `error` or `rejected`.

Colors auto-disable when output is piped or `NO_COLOR` is set.

## Shutdown

Send `SIGINT` or `SIGTERM` to the switcher. On the first signal, it runs the optional shutdown hook, then gracefully stops the active upstream model process group (SIGTERM + 3s grace, then SIGKILL), including servers launched through wrapper scripts. A second `SIGINT` or `SIGTERM` cancels the main server task; owned cleanup can still delay exit. This signal policy is unchanged and does not guarantee immediate forced exit. SIGKILL, power/network failure, and forced service termination cannot guarantee hooks complete. Cleanup ownership is in memory only, with no persistent recovery after restart.
