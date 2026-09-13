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

- `GET /stats` — human-readable dashboard: auto-refreshing HTML page with summary cards, an error breakdown, and a per-model table.
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
    "bytes_in": 1024, "bytes_out": 40960
  },
  "throughput": {"last_60s": 5, "last_300s": 12, "avg_rpm": 0.7},
  "swaps": {"count": 2, "avg_ms": 1500.0, "p95_ms": 2100.0, "max_ms": 2100.0},
  "models": {"llama-8b": {"starts": 1, "stops": 0, "requests": {"...": "..."}}}
}
```

Metric semantics:

- Counted requests are proxied requests plus rejected ones; `GET /v1/models`, `GET /models`, and the stats endpoints themselves are excluded.
- Success means the final response status is below 400 with no mid-stream error. Failures are keyed by kind: rejection kinds (`"400"`, `"404"`, `"503"`, `"504"`), upstream statuses (`"500"`, `"502"`, ...), mid-stream SSE errors (`"stream_error"`), and exceptions (`"error"`). Null fields (`success_rate`, `latency_ms`, swap durations) mean "no data yet".
- Latency is the upstream round-trip only: it excludes swap-queue wait and model-load time, so a request that triggers a cold start or swap reports its post-load latency.
- Swaps count every stop-then-start in the model manager, including same-model restarts and crash recovery; swap durations are recorded only when the swap completes with a healthy model.

Known limitations: these paths shadow upstreams serving the same paths (like `/v1/models`); latency samples are unbounded in memory (fine for a local dev tool); requests naming unknown models each create a per-model stats row keyed by the client-supplied name.

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to YAML config |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Listen port |
| `--timeout` | `60` | For positive values: total swap/non-stream timeout and stream connection/read-inactivity timeout (seconds); streams have no total lifetime limit |
| `--idle-timeout` | `30m` | Unload idle model after duration (`900`, `900s`, `15m`, `1h`; `0` disables) |
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

Send `SIGINT` or `SIGTERM` to the switcher. On the first signal, it gracefully stops the active upstream model process group (SIGTERM + 3s grace, then SIGKILL), including servers launched through wrapper scripts. If shutdown appears stuck, send a second `SIGINT` or `SIGTERM` to force shutdown by cancelling the main server task.
