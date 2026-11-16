# Command line and API endpoints

Every command-line flag with its default, stream timeouts and keepalives, upstream connection reuse, and the model-list and proxy endpoints. Control routes are in [CONTROL](CONTROL.md#control-panel-and-runtime-api); statistics routes are in [STATISTICS](STATISTICS.md).

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to YAML config |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Listen port |
| `--timeout` | `60` | For positive values: total swap/non-stream timeout and stream connection/read-inactivity timeout (seconds); streams have no total lifetime limit |
| `--idle-timeout` | `30m` | Unload idle model after duration (`900`, `900s`, `15m`, `1h`; `0` disables) |
| `--automatic-switch` | off | Let a request for another model replace the running model; when off, a request still loads its model if none is running ([details](CONTROL.md#automatic-switching)) |
| `--quick-switch` | off | Let a newer different-model request supersede a model that is still loading |
| `--sse-keepalive` | `15` | Seconds without stream data before the switcher sends the client an SSE keepalive comment (fractions allowed; `0` disables) |
| `--upstream-keepalive` | `4` | Seconds an idle pooled connection to the upstream stays open before the switcher closes it (fractions allowed; must be above `0`) ([details](#upstream-connections)) |
| `--timestamp` | off | Include timestamps in log output |
| `--version` | — | Print version |

For positive `--timeout` values, model swaps and non-streaming requests must finish within the configured total duration. Streaming requests may continue for any total duration while data keeps arriving; establishing the upstream connection or waiting between chunks for longer than the configured duration ends the stream through the existing error path.

Some engines go quiet for a long time mid-stream (for example while building a tool call they cannot stream), and clients or proxies in between may drop an idle connection. With `--sse-keepalive N`, the switcher sends `: keepalive` (an SSE comment; clients that follow the SSE spec ignore it) after every N quiet seconds. It does this only after the upstream has sent its response headers (the model load wait is not covered), only for `text/event-stream` responses, and only between events, never inside one. Keepalives do not count in stats and do not reset the `--timeout` limit: an upstream that sends nothing for longer than `--timeout` still ends the stream with an error.

```bash
./run.sh --config config.yaml --sse-keepalive 10   # every 10 s of quiet
./run.sh --config config.yaml --sse-keepalive 0    # off
```

### Upstream connections

The switcher keeps idle connections to the upstream open for reuse, for `--upstream-keepalive` seconds (default 4). Keep this value below the upstream's own idle keep-alive limit. vLLM (uvicorn) closes idle connections after 5 s; if the switcher reuses a connection at the moment the upstream closes it, sending fails (for example `[Errno 32] Broken pipe`).

If sending fails this way before the upstream answers, the request is retried once on another connection, so it reaches the upstream at most twice. For `GET`, `HEAD`, `PUT`, `DELETE`, `OPTIONS`, and `TRACE` the HTTP client library (aiohttp) does this retry itself; for other methods, such as `POST`, the switcher does it and logs a warning. A second failure returns the usual 502. Failed new connections, timeouts, and errors after the response headers arrived are not retried.

Trade-off: if the upstream received the whole request and then dropped the connection before answering, the request runs twice. For non-streaming requests the engine sends its headers only after generation finishes, so an engine that crashes or drops the connection mid-generation is retried too. The retry then usually gets "connection refused" and returns 502, or, if the engine is still up, generates the answer a second time.

```bash
./run.sh --config config.yaml --upstream-keepalive 2   # upstream closes idle connections after 3 s
```

## API Endpoints

- `GET /v1/models` — list available models (from the config, updated by a [reload](CONTROL.md#reloading-the-config); a [variant model](CONFIGURATION.md#model-variants) appears once under its public name)
- `GET /models` — alias for `/v1/models`
- `* /{path:.*}` — proxied to the active upstream model
