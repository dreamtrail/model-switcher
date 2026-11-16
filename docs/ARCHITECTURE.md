# How the switcher works

The request flow from a client's `model` field to a running upstream, idle unloading, log output, and what happens on shutdown. Service stop budgets for shutdown hooks are in [SETUP](SETUP.md#shutdown-hook-stop-budgets).

## How It Works

1. Client sends a request with a `model` field.
2. Switcher maps the name to the model to run (the active variant for a [variant model](CONFIGURATION.md#model-variants)) and checks if it is running.
3. If not (or a different model is active), it stops the current one and starts the requested model.
4. It waits for the new model to report healthy (`/health` endpoint).
5. The request is proxied to the upstream server. Request headers are forwarded except hop-by-hop headers and `Host`: the upstream gets its own address (`127.0.0.1:<port>`) as `Host`, like a default nginx proxy, so backends that reject unknown host names (such as [Strata](CONFIGURATION.md#strata)) work when clients use the switcher's network name.
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

Backends that print plain lines without a level field get their own rules, described in their sections: [TabbyAPI](CONFIGURATION.md#tabbyapi), [Strata](CONFIGURATION.md#strata), [TensorFold](CONFIGURATION.md#tensorfold), and [Splash](CONFIGURATION.md#splash).

Colors auto-disable when output is piped or `NO_COLOR` is set.

## Shutdown

Send `SIGINT` or `SIGTERM` to the switcher. On the first signal, it runs the optional shutdown hook, then gracefully stops the active upstream model process group (SIGTERM, then the model's [stop grace](CONFIGURATION.md#stop-grace-period) of 3 s by default, then SIGKILL), including servers launched through wrapper scripts. A second `SIGINT` or `SIGTERM` cancels the main server task; owned cleanup can still delay exit. This signal policy is unchanged and does not guarantee immediate forced exit. SIGKILL, power/network failure, and forced service termination cannot guarantee hooks complete. Cleanup ownership is in memory only, with no persistent recovery after restart.
