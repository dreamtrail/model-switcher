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

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to YAML config |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Listen port |
| `--timeout` | `60` | Swap/request timeout (seconds) |
| `--idle-timeout` | `30m` | Unload idle model after duration (`900`, `900s`, `15m`, `1h`; `0` disables) |
| `--version` | — | Print version |

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

Upstream model logs keep the timestamp and model-name prefix (for example, `2026-06-19 10:30:06 llama-3.1-8b: ...`) and omit Python logger names such as `src.process_manager`. Normal upstream output omits the switcher's `[INFO]` label and color; warning/error markers from upstream remain formatted as `[WARNING]` or `[ERROR]`. Output written to stderr is not automatically treated as a warning; severity is inferred from common upstream markers when possible, and otherwise uses the normal upstream format.

Colors auto-disable when output is piped or `NO_COLOR` is set.

## Shutdown

Send `SIGINT` or `SIGTERM` to the switcher. On the first signal, it gracefully stops the active upstream model process group (SIGTERM + 3s grace, then SIGKILL), including servers launched through wrapper scripts. If shutdown appears stuck, send a second `SIGINT` or `SIGTERM` to force shutdown by cancelling the main server task.
