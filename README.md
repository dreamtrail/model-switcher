# Model Switcher

Automatic model swapping reverse proxy for local AI servers. Point your client at the switcher, and it handles starting, stopping, and routing to the correct model — no manual intervention needed.

By default a request loads its model when none is running, but does not replace a running model with another one. Start with `--automatic-switch` (or turn it on in the control panel) to let requests switch models: see [Automatic switching](docs/CONTROL.md#automatic-switching).

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

## Features

- Starts the requested model on first use, routes requests by their `model` field, and by default unloads it after 30 idle minutes to free VRAM ([how it works](docs/ARCHITECTURE.md)).
- Runs any server that stays in the foreground and takes a port; recipes for SGLang, NInfer, TabbyAPI, Strata, TensorFold, and Splash ([backend recipes](docs/CONFIGURATION.md)).
- One model name with several launch commands (variants), optional shutdown hooks, and a per-model stop grace period ([configuration](docs/CONFIGURATION.md)).
- Browser control panel and HTTP API to load, unload, pick variants, change settings, and reload the config without a restart ([control panel](docs/CONTROL.md)).
- Statistics dashboard at `/stats` and JSON at `/stats/json`: speeds, tokens, cache hits, latency, and loads ([statistics](docs/STATISTICS.md)).
- Streams (SSE) are passed through in real time, with keepalive comments during long quiet gaps (`--sse-keepalive`, default 15 s; [CLI flags](docs/CLI.md)).

## Documentation

- [CONFIGURATION](docs/CONFIGURATION.md): the YAML config, shutdown hooks, stop grace, model variants, and launch recipes for each backend.
- [CONTROL](docs/CONTROL.md): automatic and quick switching, the `/control` panel, the runtime API, and config reload.
- [STATISTICS](docs/STATISTICS.md): what `/stats` and `/stats/json` show and how each speed is measured.
- [CLI](docs/CLI.md): command-line flags, timeouts, SSE keepalives, upstream connections, and API endpoints.
- [ARCHITECTURE](docs/ARCHITECTURE.md): request flow, idle VRAM release, log output, and shutdown.
- [SETUP](docs/SETUP.md): requirements and running as a systemd user service (linger, reload, stop budgets).
- [DECISIONS](docs/DECISIONS.md): why the design is the way it is, by plan ID.
- [TODO](docs/TODO.md) and [TASKLOG](docs/TASKLOG.md): open tasks and completed work.
- [HANDOFF](docs/HANDOFF.md): the current session state for the next person or agent.
