# Model Switcher

Automatic model swapping reverse proxy for local AI servers. Point your client at the switcher, and it handles starting, stopping, and routing to the correct model — no manual intervention needed.

By default a request loads its model when none is running, but does not replace a running model with another one. Start with `--automatic-switch` (or turn it on in the control panel) to let requests switch models: see [Automatic switching](#automatic-switching).

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

Completed work is recorded in [the task log](docs/TASKLOG.md).

Design choices and their history are recorded in [the decision index](docs/DECISIONS.md).

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
- A command whose first word is a path to a **text file** is a script: the switcher runs a temp copy with the port rewritten. A path to a program (`/opt/llama/llama-server --model m.gguf`) is a direct command, the same as one found on `PATH`: its `--port` flag is replaced, or `--port <N>` is added. Text means UTF-8 without NUL bytes; anything else counts as a program.
- Model and variant names must be strings. Quote a name that YAML reads as a number, a boolean, null, or a date, such as `"70"`, `"yes"`, `"null"`, or `"2024-01-01"`; otherwise the switcher refuses the config (at startup and on reload) and names the value, for example `Model name 70 (int) must be a string`.
- One name can have several launch commands (for example text and vision): see [Model variants](#model-variants).
- A slow server can get more time to stop cleanly before SIGKILL: see [Stop grace period](#stop-grace-period).
- After editing the file, apply it without a restart: see [Reloading the config](#reloading-the-config).

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

### Stop grace period

When a model stops (swap, unload, idle unload, or switcher shutdown), the
switcher sends SIGTERM to its process group, waits, then sends SIGKILL to
whatever is left. The wait is 3 seconds unless the entry sets `stop_grace`
(seconds):

```yaml
my-model:
  cmd: "my-model-server --port ${PORT}"
  stop_grace: 20
```

`stop_grace` must be a positive finite number (not a boolean) and does not
need `shutdown_cmd`. Set it on a mapping entry or inside a variant, not next
to `variants`. The wait ends as soon as the process group is gone, so a large
value costs time only when the server is slow to exit. It runs after the
optional shutdown hook, so include it in any service stop timeout. A running
model keeps the value it started with; a [reload](#reloading-the-config)
applies a new value at its next start. The recipes for [Strata](#strata)
(`stop_grace: 25`), [TensorFold](#tensorfold) on MLX (`stop_grace: 130`), and
[Splash](#splash) (`stop_grace: 20`) set it; each section explains its value.

### Model variants

A variant entry gives one model name several launch commands. Clients (for
example an agent harness) always send the same name; you pick which variant
runs from the [control panel or API](#control-panel-and-runtime-api). The
client never sees the switch.

```yaml
qwen3.8-27b:
  default: text            # optional; the first variant if omitted
  variants:
    text: "llama-server --model /path/to/qwen3.8-27b.gguf --port ${PORT} -a qwen3.8-27b"
    vision:
      cmd: "other-server --model /path/to/qwen3.8-27b-vl --port ${PORT} --served-model-name qwen3.8-27b"
      shutdown_cmd: "/path/to/stop-other-server"
      shutdown_timeout: 30
```

- Each variant is a command string or a mapping with `cmd` and optional
  `shutdown_cmd` / `shutdown_timeout` / `stop_grace`, with the same rules as a plain entry.
  Each variant gets its own port and its own [shutdown hook](#optional-shutdown-hooks).
- Only `variants` and `default` may sit beside each other at the top of the
  entry; `cmd`, `shutdown_cmd`, `shutdown_timeout`, or any other key there is a
  config error. Unknown keys inside a variant are errors too. `default` must name
  a variant, and `default` without `variants` is an error. Model and variant
  names must not contain `@`.
- Inside the switcher each variant is a separate model with the id
  `name@variant` (`qwen3.8-27b@vision`). That id appears in `active_model`,
  `pending_model`, logs, and [statistics](#statistics) rows (one row per variant).
  Clients cannot use it: a request for `qwen3.8-27b@vision` gets 404.
- A request for `qwen3.8-27b` goes to the active variant, which starts as
  `default`. Switching the variant stops the running one (its own hook first),
  starts the new one, and sends later `qwen3.8-27b` requests to it.
- The request body is forwarded unchanged, so every variant's server must
  accept the public name (`-a`, `--served-model-name`, or the like).
- `GET /v1/models` lists `qwen3.8-27b` once, not the variants.
- The chosen variant is kept in memory only. A [config reload](#reloading-the-config)
  keeps it (unless that variant was removed), even if you changed `default`;
  a restart starts each entry on its `default`.

## Quick Switching

These rules apply to requests while [automatic switching](#automatic-switching) is on, and to manual loads. With automatic switching off (the default), a request for a different model than the one loading gets `automatic_switch_disabled` instead.

By default, a request for the model that is already loading joins that load. A request for a different model is refused immediately with HTTP 409 and `error.code: "switch_in_progress"`; it is not queued to trigger another slow load afterward.

Enable quick switching when a newer different-model request should replace the pending target:

```bash
./run.sh --config config.yaml --quick-switch
```

With `--quick-switch`, the latest admitted model wins. Requests for the same target still join one load, while requests made obsolete by a newer target receive HTTP 409 with `error.code: "swap_superseded"`. Supersession interrupts readiness polling only: the obsolete model's process group is stopped before its replacement starts. If the requested model had already been stopped, switching back loads it again.

Process creation failures return HTTP 500 with `error.code: "model_start_failed"`. If an optional shutdown hook fails, or SIGTERM, the existing grace period, and SIGKILL cannot confirm that the old process group exited, switching fails closed with HTTP 503 and `error.code: "process_cleanup_failed"`; no replacement starts until cleanup is confirmed. Graceful cleanup can therefore delay a quick switch by several seconds, but prevents overlapping model servers and GPU-memory contention.

## Control Panel and Runtime API

Open `http://localhost:8080/control` to use the dependency-free control panel on the existing listener. It shows configured, active, and pending models, lifecycle state, in-flight request count, and the three runtime settings. It can load or switch to a model, unload the active model, update settings, and [reload the config file](#reloading-the-config). A [variant model](#model-variants) shows its name with one button per variant: the loaded variant has a "Loaded" badge, and the active variant (where requests go) has an "Active" badge when it is not loaded; clicking a variant makes it active and loads it. It uses the same look as the statistics dashboard (light and dark theme, status tiles, cards); the header links to `/stats` (the dashboard links back to `/control`). It never displays or edits model commands, assigned ports, process IDs, environment values, or the config path.

> **Network trust warning:** the control API has no authentication or authorization. `X-Model-Switcher-Control: 1` protects browsers against cross-site request forgery; it does **not** prove who the caller is. Binding model-switcher to `0.0.0.0` or another shared interface grants every network-reachable client process-control authority. Use this only on a trusted local network. If the listener is exposed beyond that boundary, put an authenticated service in front of it or do not expose it.

The switcher-local routes are:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/control` | Browser control panel |
| `GET` | `/api/v1/control/state` | Full runtime state |
| `PUT` | `/api/v1/control/settings` | Replace all three runtime settings |
| `PUT` | `/api/v1/control/model` | Load or switch to one configured model (or variant) |
| `DELETE` | `/api/v1/control/model` | Unload the tracked model |
| `POST` | `/api/v1/control/reload` | Re-read the config file ([details](#reloading-the-config)) |

`/control` and every path at or below `/api/v1/control` are reserved and intentionally shadow matching upstream routes. State responses use `Cache-Control: no-store`. Every mutation requires `X-Model-Switcher-Control: 1`; PUT requests also require `Content-Type: application/json` and a body no larger than 4 KiB. DELETE and POST are bodyless. The service emits no CORS permission headers.

Example state:

```json
{
  "models": ["llama-3.1-8b", "mistral-7b", "qwen3.8-27b"],
  "variants": {
    "qwen3.8-27b": {"variants": ["text", "vision"], "default": "text", "active": "text"}
  },
  "active_model": "llama-3.1-8b",
  "pending_model": null,
  "lifecycle": "ready",
  "inflight_requests": 0,
  "settings": {
    "automatic_switch": false,
    "quick_switch": false,
    "idle_timeout_seconds": 1800.0
  }
}
```

`models` lists the names clients send. `variants` has one entry per [variant model](#model-variants) (empty `{}` when there are none). `active_model` and `pending_model` are model ids, so a variant shows as `qwen3.8-27b@text`.

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

curl -X PUT http://127.0.0.1:8080/api/v1/control/model \
  -H 'Content-Type: application/json' \
  -H 'X-Model-Switcher-Control: 1' \
  -d '{"model":"qwen3.8-27b","variant":"vision"}'

curl -X DELETE http://127.0.0.1:8080/api/v1/control/model \
  -H 'X-Model-Switcher-Control: 1'
```

The model body is `{"model": name}` or, for a variant model, `{"model": name, "variant": variant}`. With `variant`, that variant becomes active in the same step that admits the load, so requests arriving after it go to the new variant (they join its load instead of getting `switch_in_progress`). If the load is refused before it starts (for example 409 `switch_in_progress` without quick switching, or `control_busy`), the active variant does not change. If the load starts and then fails, the new choice stays. `{"model": name}` alone loads the active variant. An unknown variant returns 404 `unknown_variant`; `variant` on a model without variants returns 400 `model_has_no_variants`; a variant id such as `qwen3.8-27b@vision` in `model` returns 404 `unknown_model`. With quick switching, a pending request for the old variant is superseded (409 `swap_superseded`).

Changing a positive idle timeout restarts the countdown from the update time when a healthy model is idle; zero disables idle unload. Cleanup already in progress still finishes. Manual switches and unloads preserve the existing model-change behavior and may interrupt in-flight requests using the previous process; the panel shows the in-flight count but does not drain requests.

All control settings are process-local; a [config reload](#reloading-the-config) does not change them. Restart restores the command-line `--automatic-switch` value (off unless given), the command-line `--quick-switch` value, the command-line `--idle-timeout` value, and each variant model's `default` variant; no YAML is changed.

### Automatic switching

The `automatic_switch` setting decides whether a request may replace the model that is running or loading. It is off at start. Turn it on with `--automatic-switch`, or at runtime with the `/control` checkbox or `PUT /api/v1/control/settings`.

```bash
./run.sh --config config.yaml --automatic-switch
```

With it off (the default):

- **Nothing running or loading** — at start, after an idle or manual unload, after the model process exits, or after a failed load, a request loads its model, the same as with it on. More requests for that model join the load.
- **A model running or loading** — requests for that model and model-less requests go to it. A request for any other model gets HTTP 409 with `error.code: "automatic_switch_disabled"` and changes nothing, also with `--quick-switch`. While a manual switch replaces one model with another, only the incoming model counts.
- "Running" means the process is alive and passed its health check. A model whose process exited, or whose stop failed (lifecycle `cleanup_failed`), is not running, so a request may load a model; after a failed stop the switcher first retries the stop and returns 503 `process_cleanup_failed` if it fails again.

With it on, a request for another model stops the running model and loads the requested one; during a load, the [quick switching](#quick-switching) rules apply.

Model-less requests with no model running get 400 either way. Unknown models remain 404. Manual model controls are not limited by this setting: they still share the same bounded admission, latest-wins quick-switch setting, cleanup, readiness, and shutdown rules. A manual unload returns 409 `control_busy` while a load/switch or another unload is pending. Turning automatic switching off does not cancel work already admitted.

### Reloading the config

Edit the config file, then click **Reload config** on `/control` or call:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/control/reload \
  -H 'X-Model-Switcher-Control: 1'
```

The switcher re-reads the file it was started with (`--config`) and applies models, variants, commands, and shutdown hooks without a restart. The reply is the full state document plus a `reload` summary of model ids:

```json
"reload": {"added": ["gamma"], "removed": ["beta"], "changed": ["alpha"], "running_changed": true}
```

- **New models** are listed in `/v1/models` and can be requested right away.
- **Removed models** get 404 for named requests at once.
- **Changed models** are entries whose `cmd`, `shutdown_cmd`, `shutdown_timeout`, or `stop_grace` changed. They get a new port and, for scripts, a new temp copy; the old copy is never overwritten. If the command starts with a script file, the script's contents count too: editing the script changes the entry even when its config line stays the same.
- **Unchanged models** keep their port, command, and temp copy. The config text and a hash of the script file (when the command starts with one) are compared.
- **The running model is not touched** (`running_changed: true` when it was changed or removed). It keeps its old command, port, and hook until it stops; its next start uses the new settings. A removed running model keeps serving model-less requests until you switch or unload it, or the idle timeout stops it. The panel shows a note in both cases.
- **Variants:** the active variant stays active if it still exists, otherwise the entry's `default`. A changed `default:` alone does not switch the active variant, even if that variant was only active because it was the old default; pick the variant on `/control` (or restart) to use the new default.
- **Not reloaded:** command-line flags and the [runtime settings](#control-panel-and-runtime-api) above. The YAML file is only read, never written.

If the file is missing or invalid, nothing changes and the old config keeps working; the reply is 400 `config_invalid` with the error, for example `Config not reloaded: Model 'a' has an empty command` or `Config not reloaded: Invalid YAML in config.yaml: mapping values are not allowed here (line 2, column 4)`. The message names only the config file (not its full path) and never quotes source lines. A reload while a model is loading or unloading (including an idle unload) returns 409 `control_busy` (try again after it finishes); during shutdown it returns 503 `shutting_down`.

**SIGHUP** does the same reload: `kill -HUP <pid>` re-reads the file and logs `SIGHUP: config reloaded (added 1, removed 0, changed 1)`, or a warning with the reason when it did not reload (invalid file, or a model is loading or unloading: send it again later). Once the switcher is running it never exits on SIGHUP. During startup (before it logs `Listening on`), SIGHUP still terminates it, as it did before. For systemd, add `ExecReload=/bin/kill -HUP $MAINPID` so `systemctl reload` works; the wrapper must `exec` the switcher ([unit and wrapper](docs/SETUP.md#reload-the-config-with-systemctl)). There is no file-watch trigger.

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

The API request's `model` must match the config key exactly, including case. The groupwise and NVFP4 artifacts for each 27B family share the same embedded ID, so two separate entries for both weight variants of that family would need the same config key, which is impossible. Configure them as [variants](#model-variants) of that one ID instead and pick the active artifact from `/control`; the request `model` stays the embedded ID. Model-switcher does not rewrite model IDs.

NInfer's `/health` becomes available after model loading and warm-up. Model-switcher then proxies its OpenAI-compatible JSON and SSE output unchanged: Chat Completions streams end with `data: [DONE]`, while Responses API streams use named `event:` records and a terminal response event without `[DONE]`. Upstream statuses, error bodies, and headers such as `x-request-id` are forwarded normally. On a swap or shutdown, model-switcher sends SIGTERM before its existing forced-stop fallback.

## TabbyAPI

[TabbyAPI](https://github.com/theroyallab/tabbyAPI) (ExLlamaV3) works through the same foreground-process interface. This integration targets TabbyAPI master at commit `816c321`. Install TabbyAPI in its own folder by following its upstream guide (the default install creates `venv/` in that folder) and pick an ExLlamaV3 model that fits your GPU memory; TabbyAPI is not a model-switcher dependency.

Start `main.py` from the TabbyAPI folder, bind it to loopback, and make the config key equal to the model folder name passed to `--model-name`:

```yaml
qwen3-8b-exl3: 'bash -c "cd /path/to/tabbyAPI && TABBY_LOG_CONSOLE_WIDTH=1000 exec venv/bin/python main.py --host 127.0.0.1 --port ${PORT} --model-dir /path/to/models --model-name qwen3-8b-exl3 --disable-auth true"'
```

Why the recipe looks like this:

- **`bash -c` and `cd`** — model-switcher executes commands directly rather than through a shell, and TabbyAPI reads `config.yml`, `api_tokens.yml`, and writes `logs/` relative to the current folder. The `bash -c "cd ... && exec ..."` wrapper sets that folder; `exec` makes TabbyAPI replace the shell so it receives SIGTERM directly.
- **`main.py`, not `start.sh`** — `start.sh` / `start.py` may create a venv, run `pip install`, or ask questions on first run. The switcher needs a non-interactive foreground server, so run `main.py` with the already installed venv.
- **No `--config`** — with `--config <file>` TabbyAPI drops the other command-line flags, so the assigned `--port` would be lost. Without it, TabbyAPI still reads `config.yml` in its folder, and command-line flags override values from `config.yml`.
- **Bool flags need a value** — TabbyAPI builds its flags from the config schema, so write `--disable-auth true`, not a bare `--disable-auth`.
- **`TABBY_LOG_CONSOLE_WIDTH`** — when output is piped, TabbyAPI wraps log lines at 80 columns. A wide console keeps each message on one line so model-switcher can read its level; multi-line messages (for example tracebacks) still add indented continuation lines, which are logged as info. Lines like `12:34:56.789 WARNING: ...` are logged as warnings, `ERROR:` and `CRITICAL:` as errors, and `INFO:`, `SUCCESS:`, `DEBUG:`, `TRACE:` as info. `--log-timestamps false` (no time prefix) also works.

**Model name.** TabbyAPI loads the one model given by `--model-name` and names it after that folder. With inline model loading off (the default), it ignores the request `model`; callers with admin rights get a TabbyAPI warning on every request whose `model` differs from the folder name. Keep the config key, the folder name, and the request `model` identical, and keep `inline_model_loading` off so a request cannot load a different model behind the switcher's back.

**Authentication.** TabbyAPI has no command-line flag or environment variable for a pre-set API key; keys live only in `api_tokens.yml`. The example therefore uses `--disable-auth true`. With auth disabled, every caller is a TabbyAPI admin: anyone who can reach the switcher can use TabbyAPI admin routes (for example load or unload models, or list the whole model folder) if those paths reach it. Only expose the switcher to trusted clients in that case. To keep auth on, drop `--disable-auth true` and write your own keys into `api_tokens.yml` in the TabbyAPI folder:

```yaml
api_key: your-api-key
admin_key: your-admin-key
```

Clients then send `x-api-key: <key>` or `Authorization: Bearer <key>`; model-switcher forwards both headers unchanged. Readiness still works because TabbyAPI's `/health` needs no key.

**Readiness and output.** TabbyAPI opens its port only after the startup model has loaded, then `GET /health` returns 200 with `{"status":"healthy"}`. Chat and text completion streams use `\r\n` line endings, `: ping` keep-alive comments, and end with `data: [DONE]`; model-switcher passes them through unchanged. TabbyAPI honours the switcher's automatic `stream_options: {"include_usage": true}` and sends usage (including `prompt_tokens_details.cached_tokens`) in a final chunk, so token statistics are recorded. If a client asks for `return_progress`, the prefill progress chunks do not count as the first token; time to first token starts at the first generated text.

**Ports.** If the assigned port is busy, TabbyAPI binds port + 1 instead and logs `WARNING: Port X is currently in use. Switching to X+1`, which model-switcher shows as a warning. Model-switcher probes only the assigned port, and another program can claim it before TabbyAPI starts. The switcher keeps checking and routing to that assigned port; it does not follow TabbyAPI to port + 1. Readiness then times out on the assigned port, unless that program answers `GET /health` with 200; in that case model-switcher treats it as ready and sends requests to the wrong server.

**Stopping.** On a swap or shutdown, model-switcher sends SIGTERM to the process group, waits for the existing grace period, and uses its forced-stop fallback if needed. Tensor-parallel workers run in the same process group. After a forced stop (SIGKILL), tensor-parallel runs may leave shared-memory segments in `/dev/shm`; check for and remove stale ones if memory runs short.

For a manual smoke check on real hardware, start model-switcher with the recipe above, then:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b-exl3","messages":[{"role":"user","content":"Hello"}]}'
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b-exl3","messages":[{"role":"user","content":"Hello"}],"stream":true}'
curl -N http://127.0.0.1:8080/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-8b-exl3","prompt":"Hello","stream":true}'
```

The first request waits while TabbyAPI loads the model. Confirm both streams end with `data: [DONE]` and that `/stats` shows prompt and completion tokens for the model. Automated tests use local fake servers and do not run TabbyAPI, download models, or need a GPU.

## Strata

[Strata](https://github.com/Niko1221/Strata) (a Qwen3.8-Flash-Next inference engine with a small Python HTTP front end) works through the same foreground-process interface. This integration targets Strata at commit `6f32ec0` (2026-10-04); Strata changes quickly, so newer commits may differ. Install it in its own folder with its setup script, which builds the engine and writes a `strata-<model>.json` config next to a `run-<model>.sh` script; Strata is not a model-switcher dependency.

Run `serve/server.py` from the Strata repo root and pass `--engine strata`:

```yaml
qwen3.8-flash-next:
  cmd: "env -C /path/to/Strata python serve/server.py --engine strata --config strata-qwen3.8-flash-next.json --port ${PORT}"
  stop_grace: 25
```

Why the recipe looks like this:

- **`--engine strata`** — the default engine is `mock`, which returns fake replies without loading a model.
- **`env -C`** — model-switcher executes commands directly rather than through a shell, and Strata expects to start from its repo root (its own `run-<model>.sh` does the same; the `--config` path is relative to it). `env -C <dir>` sets that folder without a shell; it needs GNU coreutils 8.28 or newer (macOS and BusyBox `env` lack `-C`).
- **`python`** — must be the Python that Strata was installed with. Debian and Ubuntu may have no `python` command, so write `python3` or the full path to Strata's venv Python (for example `/path/to/Strata/<venv>/bin/python`).
- **`--port ${PORT}`** — the switcher assigns the port. Strata already binds `127.0.0.1` by default; the model path lives in the JSON config, not on the command line.
- **`stop_grace: 25`** — gives Strata time to stop its engine cleanly; see **Stopping** below.

**Model name.** Strata serves one model and accepts any `model` name in a request, so the switcher's routing decides which model runs. Use the same name for the config key and the request `model`; it does not have to match Strata's own `model_name`.

**Readiness.** Strata opens its port only after the model has loaded, which can take minutes. Raise the switcher's `--timeout` (default 60 s) above your load time, for example `--timeout 600`. Then `GET /health` returns 200 with `"loaded": true`. A 200 with `"loaded": false` (Strata's lazy mode or after its own idle unload) counts as still loading. Leave Strata's own lazy loading and idle unload off: do not pass `--lazy` or `--idle-unload`, and check that the JSON config does not set `lazy_load` or `idle_unload_s`. The switcher already loads on demand and unloads idle models.

**Output.** Chat completions JSON and SSE pass through unchanged, including `: keep-alive` comments, `reasoning_content` deltas, and the final `data: [DONE]`. Strata ignores the switcher's automatic `stream_options` and always sends usage (with `prompt_tokens_details.cached_tokens`) plus llama.cpp-style `timings` on the final chunk, so token counts and server-timed prompt and generation speed are recorded. Strata has no `/v1/completions` or embeddings; such requests get Strata's 404. `/v1/messages` and `/v1/responses` are passed through but not covered by the tests here. Strata runs one request at a time by default and queues the rest.

**Request guards.** Without an API key, Strata refuses (403) any request whose `Host` is a DNS name other than `localhost`, to guard against DNS rebinding. The switcher already replaces `Host` with `127.0.0.1:<port>` ([How It Works](#how-it-works)), so clients can use any name, for example `http://dgx1:8080`, and you do not need `STRATA_ALLOWED_HOSTS`.

Strata's source (commit `6f32ec0`, not tested here) has two more guards for `/v1/*` POST requests without an API key: a browser `Origin` header from another site gets 403, and a non-JSON `Content-Type` gets 415. The switcher passes `Origin` through unchanged, so a web page on another site that calls the switcher directly needs an API key (`--api-key` or `STRATA_API_KEY`, sent by the client) or another setup.

**Logs.** Strata prints plain lines that start with `[strata] `. Lines with the word `WARNING` or `WARN` are logged as warnings, lines with `ERROR`, `FATAL`, or `CRITICAL` as errors, and the rest as info. Only upper-case words count, so a line such as `[strata] the engine reported an error: ...` stays info.

**Stopping.** On a swap or shutdown, model-switcher sends SIGTERM to the process group (the Python server and the engine). Strata's server then stops HTTP, tells the engine to quit, and waits up to 20 s for it to free tens of GB of memory before forcing it. With the default 3 s wait, the switcher would send SIGKILL partway through; `stop_grace: 25` covers that clean step ([stop grace period](#stop-grace-period)). The wait ends as soon as Strata exits, so a quick stop costs no extra time. Whether the engine itself exits at once on the group's SIGTERM is not known (its signal handling was not read). After a SIGKILL the operating system should still free the memory when the processes exit (expected, not tested).

For a manual smoke check on real hardware, start model-switcher with the recipe above and a long `--timeout`, then:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Hello"}]}'
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-flash-next","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

The first request waits while Strata loads the model. Confirm the reply is real text (not a mock reply), the stream ends with `data: [DONE]`, `/stats` shows prompt and generation speed for the model, and the same requests work through the switcher's network name (for example `http://dgx1:8080`). After a swap, check with `nvidia-smi` that GPU memory was freed. Automated tests use local fake servers and do not run Strata, download models, or need a GPU; this support has not yet been tried on real hardware.

## TensorFold

[TensorFold](https://github.com/ashhart/TensorFold) (an OpenAI-compatible server for Apple Silicon through MLX, and for NVIDIA GPUs through CUDA) works through the same foreground-process interface. This integration targets TensorFold at commit `609ca419` (release 0.6.5, 2026-10-03); newer releases may differ. Install it with its own instructions (for example `pip install` into its own venv, or Homebrew on macOS); TensorFold is not a model-switcher dependency.

```yaml
qwen3.8-27b-tensorfold:
  cmd: 'bash -c "PYTHONUNBUFFERED=1 exec tensorfold serve TensorFold/Qwen3.8-27B-MLX-4bit --host 127.0.0.1 --port ${PORT} --name qwen3.8-27b-tensorfold --no-update-check"'
  stop_grace: 130
```

Why the recipe looks like this:

- **`PYTHONUNBUFFERED=1`** — TensorFold prints its request lines without flushing. On the switcher's pipe they would sit in a buffer and show up late or only at exit. The switcher runs commands without a shell, so a `VAR=value` prefix needs a wrapper: here `bash -c`. `env PYTHONUNBUFFERED=1 tensorfold serve ...` works too.
- **`exec`** — bash replaces itself with TensorFold, so there is one process and the switcher's SIGTERM reaches TensorFold directly.
- **`tensorfold serve MODEL`** — `MODEL` is a Hugging Face repo id or a local folder. Write the full path to `tensorfold` (for example `/path/to/venv/bin/tensorfold`) if it is not on the switcher's `PATH`.
- **`--host 127.0.0.1 --port ${PORT}`** — the switcher assigns the port; TensorFold's default port is 8080, which would clash with the switcher.
- **`--name ID`** — the model name TensorFold reports in `/v1/models` and replies. Use the config key.
- **`--no-update-check`** — skips TensorFold's network release check at startup.
- **`stop_grace: 130`** — gives TensorFold on MLX time to save its checkpoints; see **Stopping** below.

**Model name.** TensorFold serves one model, and the request `model` is not enforced: any name works. The switcher's routing decides which model runs.

**Readiness.** TensorFold opens its port only after the model has loaded, and `GET /health` then returns 200 with `"status": "ok"`, so the existing readiness check works unchanged. The first launch also downloads the model, which can take a long time. Download it ahead with `tensorfold pull MODEL`, or raise the switcher's `--timeout` (default 60 s) above the download and load time, for example `--timeout 600`.

**Output and stats.** Chat completions JSON and SSE pass through unchanged. TensorFold supports the switcher's automatic `stream_options.include_usage`: the stream's finish chunk carries a top-level `tensorfold` object, then usage (with `prompt_tokens_details.cached_tokens`) comes in its own chunk before `data: [DONE]`. Non-streaming replies carry both on the body. TensorFold sends no llama.cpp-style `timings`, so:

- **Prompt speed** comes from the server: `prompt − cached` ÷ `tensorfold.prefill_seconds` (MLX) or `tensorfold.prefill_s` (CUDA), both in seconds. On MLX this time starts when the request is submitted, so it also includes waiting in TensorFold's own queue when several requests run at once.
- **Generation speed** comes from the switcher's own stream timing. TensorFold's `tokens_per_second` is not used, and non-streaming generation is not timed.

`/v1/completions`, `/v1/messages`, and `/v1/responses` are passed through but not covered by the tests here.

**Logs.** TensorFold prints plain lines that start with `[tensorfold] `. A message that starts with `warning:` is logged as a warning, one that starts with `error:`, `request error:`, or `stream error:` as an error (any letter case), and the rest as info. The words must come first: `[tensorfold] snapshot save failed: ...` stays info. Python tracebacks after a `request error:` line go to stderr without the prefix and are logged as info.

**Stopping.** On a swap or shutdown, model-switcher sends SIGTERM to the process group. TensorFold stops cleanly on SIGTERM, but on MLX it first saves recent conversation checkpoints to disk, which may take longer than the default 3 s wait (TensorFold allows up to 120 s; real times not measured). `stop_grace: 130` lets it finish ([stop grace period](#stop-grace-period)); the wait ends as soon as TensorFold exits, so a quick stop costs no extra time. A slow save delays the next model's start by that long, so you may prefer a lower value or none: a SIGKILL loses only those checkpoints, which are a cache, and replies already sent are not affected. The value is for MLX; how long a CUDA stop takes was not checked. Under systemd, 130 s is longer than the default 90 s stop timeout, so raise `TimeoutStopSec` to cover it (see [SETUP](docs/SETUP.md)).

**Multi-GPU.** Only single-process serving fits the switcher. TensorFold's `--tp 2` runs one process per machine (rank 0 serves HTTP, rank 1 follows); the switcher can start only rank 0 on its own machine, so `--tp 2` is not supported.

For a manual check on real hardware, start model-switcher with the recipe above and a long `--timeout`, then:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-tensorfold","messages":[{"role":"user","content":"Hello"}]}'
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-tensorfold","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

The first request waits while TensorFold loads (and maybe downloads) the model. Confirm the stream ends with `data: [DONE]`, `/stats` shows prompt and generation speed for the model, and `[tensorfold] ` request lines appear in the switcher log while the server runs. Automated tests use a local fake server and do not run TensorFold, download models, or need a GPU; this support has not yet been tried on real hardware.

## Splash

[Splash](https://github.com/incoai/splash) (an OpenAI- and Anthropic-compatible inference server for Apple silicon) works through the same foreground-process interface. It runs only on Apple M3 or newer with macOS 26.4 or later, so the switcher must run on that Mac. This integration targets Splash at commit `39a9738` (release 1.4.1); newer releases may differ. Install it with its own instructions (for example `brew install incoai/tap/splash`); Splash is not a model-switcher dependency.

```yaml
qwen3.8-27b-splash:
  cmd: "splash serve --model local/qwen3.8-27b --model-dir /path/to/Qwen3.8-27B-UD-Q4_K_M.gguf --served-model-name qwen3.8-27b-splash --port ${PORT}"
  stop_grace: 20
```

Why the recipe looks like this:

- **`splash serve --model OWNER/REPO`** — with `--model-dir`, `--model` is only the name Splash reports. Without `--model-dir`, it is the Hugging Face model (`OWNER/REPO[:VARIANT]`) that Splash downloads on first start. Write the full path to `splash` (for example `/opt/homebrew/bin/splash`) if it is not on the switcher's `PATH`.
- **`--model-dir PATH`** — serves a local GGUF file or GGUF/MLX folder without contacting the Hub, so a start does not wait for a download. Splash still takes the family's draft model from the Hub cache unless you pass `--draft-model`.
- **`--served-model-name KEY`** — Splash refuses (404 `model_not_found`) a request whose `model` is neither its `--model` name nor an alias. The switcher forwards the request body unchanged, so add the config key as an alias. Replies still report the `--model` name (Splash's `--announce-served-name` changes that).
- **`--port ${PORT}`** — the switcher assigns the port; Splash's default is 8000 (or `SPLASH_PORT`). Splash binds `127.0.0.1` by default.
- **`stop_grace: 20`** — gives Splash time to close its engine; see **Stopping** below.

**Readiness.** Splash binds its port early but accepts connections only after the engine has loaded, then prints `Ready · ...`. `GET /health` then returns 200 `{"status": "ok"}`, so the existing readiness check works unchanged. Loading a large model can take minutes, and a first start without `--model-dir` also downloads it. Raise the switcher's `--timeout` (default 60 s) above that time, for example `--timeout 600`.

**Idle memory.** Splash has its own idle release: after `--idle-release` (default `10m`) without a request, it frees the model weights but keeps running, and the next request loads them back first, so that request waits longer. `/health` stays 200 the whole time, so the switcher still sees the model as loaded. The switcher's own idle unload (`--idle-timeout`, default 30m) stops the whole process instead. Keep Splash's release if you want memory back sooner, or add `--idle-release off` to the recipe and let `--idle-timeout` alone decide. Neither choice was tried on real hardware.

**Output and stats.** Chat completions JSON and SSE pass through unchanged, including `: splash-keepalive` comments, `prompt_progress` chunks, and the final `data: [DONE]`. Splash supports the switcher's automatic `stream_options.include_usage`: the finish chunk carries llama.cpp-style `timings`, then usage (with `prompt_tokens_details.cached_tokens`) and a `metrics` object come in their own chunk. Non-streaming replies carry all three on the body. Splash's `timings` counts differ from llama.cpp's, so:

- **Prompt speed** is `metrics.prefill.tokens` ÷ `timings.prompt_ms`. Splash's `timings.prompt_n` counts cached prompt tokens too, so on a cache hit it would make prompt speed look many times higher.
- **Generation speed** is `metrics.decode.tokens` ÷ `timings.predicted_ms`. `predicted_ms` starts after Splash's first output, which can hold several tokens at once (speculative decoding); `metrics.decode.tokens` leaves those tokens out, and `timings.predicted_n` does not.

Without `metrics` (for example a stream on a path where the switcher does not add `include_usage`), the `timings` counts are used as they are. `/v1/completions`, `/v1/messages`, and `/v1/responses` are passed through but not covered by the tests here.

**Request guards.** Splash refuses (403) a request whose `Host` is not its bind address, `localhost`, `127.0.0.1`, or `::1`. The switcher sends `127.0.0.1:<port>` as `Host` ([How It Works](#how-it-works)), so clients on the LAN can use the switcher's network name (for example `http://mac:8080`). A browser `Origin` header from a site Splash does not allow also gets 403. The switcher passes `Origin` through unchanged, so a web page on another site needs Splash's `--allowed-origin`.

**Logs.** Splash prints `HH:MM:SS <event> · <details>` lines. The events `Error ·`, `Engine failed ·`, `Engine restart failed ·`, and `Engine stopped ·` are logged as errors, `Warning ·` and `Refused ·` as warnings, and the rest (`Loading`, `Ready`, `Started`, `Done`, ...) as info. The event must come right after the time, with that exact case. The native engine's `error: ...` lines, printed before it exits, are logged as errors too.

**Stopping.** On a swap or shutdown, model-switcher sends SIGTERM to the process group. Splash then closes its engine and waits up to 15 s for the native runtime, whose cache flush alone may take up to 6 s. With the default 3 s wait, the switcher would send SIGKILL partway through; `stop_grace: 20` lets Splash finish ([stop grace period](#stop-grace-period)). The wait ends as soon as Splash exits, so a quick stop costs no extra time.

For a manual check on a Mac, start model-switcher with the recipe above and a long `--timeout`, then:

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-splash","messages":[{"role":"user","content":"Hello"}]}'
curl -N http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3.8-27b-splash","messages":[{"role":"user","content":"Hello"}],"stream":true}'
```

The first request waits while Splash loads the model. Confirm the reply is real text, the stream ends with `data: [DONE]`, and `/stats` shows prompt and generation speed for the model. Send the same request again: prompt speed should not jump on the cache hit. Check that `Ready ·` and `Done ·` lines appear in the switcher log, and that a swap to another model stops Splash without a `did not exit in 20s, sending SIGKILL` warning. Automated tests use a local fake server and do not run Splash, download models, or need a Mac; this support has not yet been tried on real hardware.

## Statistics

Model-switcher tracks runtime statistics in memory (reset on restart) and serves them at two switcher-local endpoints:

- `GET /stats` — human-readable dashboard that refreshes every 5 seconds and follows the system light/dark theme. The header links to the control panel (`/control`) and shows a live indicator: "Connecting…" before the first response, "Live · updated Ns ago" after each good response, and "Connection lost · showing last data" when a refresh fails (the last good data stays on screen), or "Connection lost · retrying" if no data has arrived yet. Seven stat tiles show the loaded model, average generation speed (tok/s), the live generation rate (**Live gen t/s**, with its source and window below the value, for example "engine · 5 s"; see `live` below), requests (with errors and requests in the last minute), active requests (with peak), model loads (**Loads**: the number of loads, the average load time, and on a second line the failed loads, for example "1 failed", or else the swaps, for example "3 swaps"; see `loads` below), and uptime (with the busy time and its share on two lines below it, for example "busy 1h 4m" and "33% of uptime"; see `utilization` below); large tile numbers are shortened (for example `12.3K`, `1.23M`) with the full number on hover. A **Models** card holds one compact table with a row per model plus an **All models** totals row (Reqs, Errors, Gen t/s, Live t/s, Gen total, Prompt t/s, Prompt total, Prompt share, TTFT p50 / p95, Cache hit %, Gen tok, Prefill tok). **Gen total** and **Prompt total** show the combined speed while busy and the average number of requests running at once, for example `85.3 @2.4` (see `avg_aggregate` below); **Gen tok** and **Prefill tok** are the generated and uncached prompt token totals, shortened like the tiles with the full number on hover. **Prompt share** is the part of request time spent on prompt processing rather than generation, for example `62%` (the rest is generation), with the seconds on hover such as "Prompt 123.4 s · Gen 75.6 s over 38 requests"; it shows `–` until a request has both times (see `time_split` below). A high share means prompt processing dominates that workload (long prompts, low cache hits); a low share means generation does. The loaded model is listed first with a "Loaded" badge, and other models are sorted by request count. An **Errors** card with one chip per error kind appears only when failures exist. The page intentionally shows a subset of the metrics; the full data (latency, bytes, prompt/cached token counts, starts/stops, windows, the per-model best cohort `max_aggregate`) is in `/stats/json`.
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
      "prompt_tps": {"avg": 890.2, "avg_aggregate": {"tps": 1240.6, "concurrency": 1.4}, "server_timed_share": 0.8, "skipped_unknown_cache": 0},
      "generation_tps": {
        "avg": 38.1, "last_60s": 41.5, "last_300s": 39.0,
        "avg_aggregate": {"tps": 85.3, "concurrency": 2.4},
        "server_timed_share": 0.7, "skipped_buffered": 1,
        "live": {"tps": 84.6, "window_s": 5, "estimate": false, "source": "engine", "busy_s": 4.8, "tokens_per_chunk": null}
      }
    },
    "ttft_ms": {"count": 40, "avg": 210.0, "p50": 180.0, "p95": 640.0, "max": 700.0},
    "tpot_ms": {"count": 40, "avg": 26.3, "p50": 25.0, "p95": 30.0, "max": 31.0},
    "time_split": {"prefill_s": 123.4, "decode_s": 75.6, "prefill_share": 0.62, "generation_share": 0.38, "requests": 38}
  },
  "throughput": {"last_60s": 5, "last_300s": 12, "avg_rpm": 0.7},
  "swaps": {"count": 2, "avg_ms": 1500.0, "p95_ms": 2100.0, "max_ms": 2100.0},
  "loads": {
    "count": 3, "avg_ms": 1100.0, "p95_ms": 1600.0, "max_ms": 1600.0, "swaps": 2,
    "stop": {"count": 2, "avg_ms": 350.0, "p95_ms": 500.0, "max_ms": 500.0},
    "failed": {"swap_timeout": 1}, "abandoned": {"swap_superseded": 1}
  },
  "models": {
    "llama-8b": {
      "starts": 1,
      "stops": 0,
      "loads": {
        "count": 1, "avg_ms": 1600.0, "p95_ms": 1600.0, "max_ms": 1600.0, "swaps": 1,
        "stop": {"count": 1, "avg_ms": 500.0, "p95_ms": 500.0, "max_ms": 500.0},
        "failed": {}, "abandoned": {}
      },
      "requests": {
        "rates": {
          "generation_tps": {
            "avg": 38.1,
            "last_60s": 41.5,
            "last_300s": 39.0,
            "avg_aggregate": {"tps": 85.3, "concurrency": 2.4},
            "max_aggregate": {"tps": 107.8, "concurrency": 3},
            "live": {"tps": 84.6, "window_s": 5, "estimate": false, "source": "engine", "busy_s": 4.8, "tokens_per_chunk": 1.8}
          }
        }
      }
    }
  },
  "utilization": {"busy_seconds": 1200.2, "busy_share": 0.333}
}
```

Metric semantics:

- Counted requests are proxied requests plus rejected ones; `GET /v1/models`, `GET /models`, and the stats endpoints themselves are excluded.
- Success means the final response status is below 400 with no mid-stream error. Failures are keyed by kind: rejection kinds (`"400"`, `"404"`, `"503"`, `"504"`), upstream statuses (`"500"`, `"502"`, ...), mid-stream SSE errors (`"stream_error"`), client disconnects (`"client_disconnect"`: the client went away mid-stream; the upstream request is closed), and exceptions (`"error"`). Null fields (`success_rate`, `latency_ms`, swap and load durations) mean "no data yet".
- Latency is the upstream round-trip only: it excludes swap-queue wait and model-load time, so a request that triggers a cold start or swap reports its post-load latency.
- `utilization` (P-20260930-02) is the utilized time: `busy_seconds` is the wall-clock time with at least one request in progress, from arrival to finish, including waiting in the queue and waiting for a model to load; overlapping requests count once. It includes the current busy period. `busy_share` is `busy_seconds` divided by the switcher's own run time, measured with a monotonic clock (0–1; `null` only when that run time is 0), so after a system clock change it may differ slightly from `busy_seconds ÷ uptime_seconds`. Requests rejected as malformed (`400`) or for an unknown model (`404`) do not count; model loads with no waiting request (for example a manual load from the control panel) do not count either. It measures request time, not GPU compute time, and like the other stats it is kept in memory and resets on restart. The Uptime tile shows it under the uptime on two lines, "busy <time>" and "<N>% of uptime" (the second line is left out when the share is `null`; hover for the definition), and falls back to "since start" when the value is missing. See [docs/decisions/stats.md](docs/decisions/stats.md#p-20260930-02--utilized-busy-time) § P-20260930-02.
- `swaps` counts only successful stop-then-start loads (a previous model process was stopped first, including same-model restarts after a crash) that ended with a healthy model; its duration covers stop + start + health check. It is kept unchanged for existing consumers; the page uses `loads`.
- `loads` (P-20260930-03, global and per model under `models[name].loads`) counts every model load: one process start attempt, whatever triggered it (first request, request after an idle or manual unload, swap, control-panel load, crash recovery). `count`, `avg_ms`, `p95_ms`, and `max_ms` cover successful loads only and time them from process start until the health check passes (`null` when `count` is 0). `swaps` is how many of those loads stopped a previous model first (globally it matches `swaps.count`), and `stop` times that stop (shutdown hook + process stop) separately for those successful loads; the stop is not part of the load time. Started loads that never became healthy are counted by kind and never timed: `failed` (`model_start_failed`, `swap_timeout`, `process_cleanup_failed`) and `abandoned` (`swap_superseded`: a newer model was requested; `shutting_down`; `cancelled`: the request that owned the load went away). A request for a model that is already loaded and healthy is not a load, and a failure while stopping the previous model (before any start) is not counted as a load. A failed load creates the model's per-model entry, so a model whose only load failed (for example a control-panel load that could not start) appears as a row with 0 requests. The **Loads** tile shows the count, "avg <time>", and a second line: "<N> failed" (all failed kinds added up) when any load failed, otherwise "<N> swaps" ("1 swap") when there were swaps; the line is left out when both are 0. Hover for the full picture: the definition, the swap and failed counts (each only when above 0), and the average stop time of the previous model, which is not included in the average. See [docs/decisions/stats.md](docs/decisions/stats.md#p-20260930-03--loads-replace-swaps-on-the-page) § P-20260930-03.

Token metrics (when the upstream reports an OpenAI `usage` field):

- Token counters are exact counts read from `usage`: `prompt`, `completion`, and `cached` (from `prompt_tokens_details.cached_tokens` when present, else the llama.cpp/TabbyAPI `timings.cache_n`). Counters are recorded whenever usage is available, regardless of request status; there is no estimation (the only estimate is the labelled `live` rate below) — when usage is absent, the fields show no data.
- `prefill` is the uncached prompt work behind `rates.prompt_tps` — the server's `timings.prompt_n` when present, else `prompt − cached` — summed only for requests with a prefill time (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)).
- `cache_hit_rate` is the token-weighted lifetime ratio `cached ÷ prompt` (per model and global); it is `null` when no prompt tokens were recorded. Like the counters, it is derived from exact reported counts — no estimation. Backends that report cumulative cached counts may yield a ratio above 1; the value is reported as-is.
- For streaming requests the switcher transparently adds `"stream_options": {"include_usage": true}` to chat/completions requests (`/v1/chat/completions`, `/v1/completions`) whose clients did not set `stream_options`, so token metrics are available even when the client doesn't ask for them. Client-set values are forwarded untouched; non-JSON and non-streaming bodies pass through unchanged.
- `ttft_ms` is streaming-only; `tpot_ms`, `rates.generation_tps` (except the windows), and the generation aggregates also cover non-streaming requests when the server reports its own decode time (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)):
  - `ttft_ms` (time to first token) — from proxy start to the first chunk with generated output (content, reasoning, tool calls, or completions text; role-only, empty, and prefill-progress chunks do not count); captures queueing, prompt processing, and model behavior. Percentiles, not averages.
  - `tpot_ms` (time per output token) — decode time divided by `(tokens - 1)` for the same decode sample as `rates.generation_tps`; requires at least 2 tokens.
  - `rates.generation_tps` — generated tokens per second of decode time (token-weighted), using the server's own decode time when available and the switcher's first-chunk → end time otherwise; the trailing `last_60s`/`last_300s` windows are completion tokens ÷ window length for every request. `server_timed_share` is the fraction of decode tokens timed by the server (`null` before any) and `skipped_buffered` counts streams left out because all their output arrived in one chunk with no server decode time; `rates.prompt_tps` — uncached prompt tokens per second of prefill time, using the server's own prefill time when available and time-to-first-token otherwise (see [Accurate prompt and generation speed](#accurate-prompt-and-generation-speed)). The two rates are never blended.
  - Per-model `rates.generation_tps.max_aggregate` is separate from those average and rolling rates. It retains the highest completed decode cohort as `{"tps": number, "concurrency": integer}` until restart; it is not added to the global generation-rate object.
  - A cohort is a maximal connected group of positively overlapping decode intervals. A switcher-timed interval runs from a stream's first output chunk to its end; a server-timed one is the server's decode time ending at the response end (clamped to the request start), so non-streaming requests take part too. Its aggregate rate is the sum of the requests' decode tokens divided by the cohort's wall-clock span; concurrency is the peak number of simultaneously decoding eligible requests. Merely touching intervals are separate.
  - Only successful requests with a generation-speed sample (see `rates.generation_tps`) and a positive decode interval contribute. A cohort remains unpublished while any in-flight request of that model that started before the cohort ended could still add an overlapping interval; failed requests are released when they end and never join a cohort. The dashboard no longer shows this value (P-20260925-04); it stays in the JSON.
  - `rates.generation_tps.avg_aggregate` (per model and global) is the busy-time average over all completed cohorts: `tps` = total cohort tokens ÷ total cohort span, and `concurrency` = total per-stream decode time ÷ total cohort span (the average number of streams decoding at once). Only time with at least one eligible stream decoding counts, so idle gaps do not lower it. One stream alone gives `concurrency` 1.0 and the same `tps` as that stream. It is `null` until a cohort closes. The global value sums the per-model totals: busy time on different models is added, not merged into one timeline, which is fine because only one model is served at a time.
  - `rates.prompt_tps.avg_aggregate` is the same busy-time average for prompt processing. Each request's prefill interval runs from proxy start to its first content chunk (its TTFT) and carries its uncached prompt tokens; with a server prefill time it is that time ending at the first token (streaming) or starting at the request start (non-streaming, capped at the response end). Eligibility follows `prompt_tps.avg` plus a positive prefill interval. The global value sums the per-model totals in the same way. Overlapping intervals form groups; a group is published only when no in-flight request of that model could still overlap it (one that started before the group ended and has no first token yet, or whose first token came after the group started). Requests that fail or never produce a first token are released and never hold a group once they end.
  - The dashboard renders both as `<tps> @<concurrency>` with one-decimal concurrency — **Gen total** with one-decimal tps (for example `85.3 @2.4`) and **Prompt total** as a whole number like **Prompt t/s** (for example `2,140 @1.8`) — and `–` before any group is published. Like `max_aggregate`, a server that is never idle publishes nothing until a gap occurs.
  - `rates.generation_tps.live` (per model and global) is the combined generation speed over the last 5 seconds, reported as `{"tps": number, "window_s": 5, "estimate": bool, "source": "engine" | "estimate", "busy_s": number, "tokens_per_chunk": number}`. `tps` is `0` while nothing is generating (also while a prompt is still being processed), and the dashboard then shows `–`. Busy time (`busy_s`) is how long the model was generating in the window, with overlapping streams counted once (floor 1 s). The value comes from one of two sources:
    - `"engine"` (vLLM): while a reply is generating, the switcher reads the backend's `/metrics` about once per second and divides the rise of `vllm:generation_tokens_total` by the busy time. This counts tokens even when a stream sends nothing for a while (for example a buffered tool call). The poll goes straight to the model process with a 1 s timeout, never delays requests, and stops when generation stops; a timeout or error is retried, while a missing endpoint or counter switches that model process to the estimate. SGLang and llama.cpp counters update only when a request finishes, so those backends use the estimate (P-20261114-01).
    - `"estimate"` (all other cases, and the first second of a reply): the one exception to the exact-counts rule (P-20260926-01, P-20260926-04). Calibrated tokens are streamed chunks × the model's tokens per chunk, learned from the exact `completion_tokens` of its last 50 finished streams (`1.0` before any). A chunk is one choice in an OpenAI-style `data:` event whose delta carries `content`, `reasoning_content`, `reasoning`, or `tool_calls` (or `text` for `/v1/completions`). The ratio lags when chunk packing changes, and an open but silent stream counts as busy. Anthropic `/v1/messages` and `/v1/responses` named-event streams and non-streaming requests are not counted, and streams whose output arrived in a single chunk are not used for calibration.
    - Global: the loaded model's engine rate when it has one and no other model is generating; otherwise all models' calibrated tokens divided by their combined busy time. The global `tokens_per_chunk` is `null`. Unlike `last_60s`, which counts exact tokens only when a request ends, the live rate moves while a stream is running. Details: [docs/decisions/stats.md](docs/decisions/stats.md#p-20261114-01--live-generation-rate-engine-counter-and-5-s-window) § P-20261114-01 (engine source) and [§ P-20260926-04](docs/decisions/stats.md#p-20260926-04--live-generation-rate-busy-time-and-calibration) (busy time and calibration).
- `time_split` (per model and global, P-20260930-01) shows how request time divides between prompt processing and generation: `prefill_s` and `decode_s` are the summed prefill and decode seconds from the subset of **Prompt t/s** and **Gen t/s** samples whose requests have both times known (so usually smaller than the totals behind those speeds), `prefill_share` and `generation_share` are fractions of their sum (0–1, adding up to 1; `null` before any counted request), and `requests` is how many requests were counted. Only successful requests with **both** a prefill and a decode sample count, so both sides cover the same requests; requests left out of either speed (unknown cache, buffered stream, non-streaming without server times, fully cached prompts, failures) are left out of both. Each request adds its own seconds, so overlapping requests each count. SGLang prefill time is the switcher's time to first token and includes queueing; swap and model-load time is not included. The global value is kept alongside the per-model values and equals their sum. See [docs/decisions/stats.md](docs/decisions/stats.md#p-20260930-01--prompt-vs-generation-time-split) § P-20260930-01.
- Non-streaming requests contribute token counts, plus prompt speed and generation speed (with **Gen total** and `max_aggregate`) when the server reports its own prefill or decode time; the switcher cannot split a single whole-body response into prompt and generation time by itself.

### Accurate prompt and generation speed

**Prompt t/s** (`rates.prompt_tps`) uses the best prefill time each response offers, in this order (P-20260929-01):

1. `timings.prompt_n` ÷ `timings.prompt_ms` (llama.cpp, TabbyAPI, Strata) — the server's own count of new prompt tokens and prefill time. Splash's `prompt_n` includes cached tokens, so for Splash the count is `metrics.prefill.tokens` (from the later usage chunk) instead.
2. `prompt − cached` ÷ `usage.prompt_time` (TabbyAPI), or ÷ `tensorfold.prefill_seconds` / `tensorfold.prefill_s` (TensorFold MLX / CUDA).
3. `prompt − cached` ÷ `metrics.time_to_first_token_ms` (vLLM).
4. `prompt − cached` ÷ the switcher's time to first token (streaming only). This also includes queueing, tokenization, the first decode step, and network time, so it reads lower than the server's own number.

`cached` comes from `usage.prompt_tokens_details.cached_tokens`, else `timings.cache_n`. For options 2–4, a missing cached count means 0 only if the model has reported one before; a model that has never reported one is left out of **Prompt t/s**, **Prompt total**, and **Prefill tok** (otherwise a fully cached prompt would look sky-high), and the request is counted in `rates.prompt_tps.skipped_unknown_cache`. `rates.prompt_tps.server_timed_share` is the fraction of prefill tokens timed by the server (`null` before any). Start the backend with these flags for exact numbers:

| Backend | Flags |
|---|---|
| llama.cpp | none |
| TabbyAPI (ExLlamaV3) | none |
| vLLM | `--enable-prompt-tokens-details --enable-per-request-metrics` |
| SGLang | `--enable-cache-report` (SGLang reports no prefill time, so the rate uses time to first token) |
| Strata | none |
| TensorFold | none |
| Splash | none |

**Gen t/s** (`rates.generation_tps.avg`, plus `tpot_ms`, **Gen total**, and `max_aggregate`) likewise uses the server's own decode time first, for streaming and non-streaming requests alike (P-20260929-03):

1. `timings.predicted_n` ÷ `timings.predicted_ms` (llama.cpp, TabbyAPI, Strata). For Splash the count is `metrics.decode.tokens` instead, which leaves out the first output that `predicted_ms` does not cover.
2. `completion_tokens` ÷ `usage.completion_time` (TabbyAPI).
3. `completion_tokens` ÷ `metrics.generation_time_ms` (vLLM).
4. `completion_tokens` ÷ the switcher's time from the first output chunk to the stream end (streaming only), used only when the output arrived in at least two chunks. A stream whose whole output came in one chunk — for example a TabbyAPI tool call, which is held back and sent in the final chunk — would otherwise look impossibly fast, so it is left out and counted in `rates.generation_tps.skipped_buffered`.

Speed is tokens ÷ time (N tokens, not N − 1). Flags for exact generation speed:

| Backend | Flags |
|---|---|
| llama.cpp | none |
| TabbyAPI (ExLlamaV3) | none |
| vLLM | `--enable-per-request-metrics` |
| SGLang | none available: streaming uses the switcher's own timing; non-streaming generation is not timed |
| Strata | none |
| TensorFold | none available: streaming uses the switcher's own timing; non-streaming generation is not timed |
| Splash | none |

Known limitations: these paths shadow upstreams serving the same paths (like `/v1/models`); latency samples are unbounded in memory (fine for a local dev tool); requests naming unknown models each create a per-model stats row keyed by the client-supplied name (a client-sent variant id such as `name@variant` is counted only in the global errors, not in that variant's row); the pending SSE line buffer is capped (~64 KiB) — once the cap is hit, non-line-shaped content (e.g. a binary audio stream without newlines) is discarded, so token metrics may be absent for such streams (byte counts remain exact).

## CLI Reference

| Flag | Default | Description |
|------|---------|-------------|
| `--config` | (required) | Path to YAML config |
| `--host` | `127.0.0.1` | Bind address |
| `--port` | `8080` | Listen port |
| `--timeout` | `60` | For positive values: total swap/non-stream timeout and stream connection/read-inactivity timeout (seconds); streams have no total lifetime limit |
| `--idle-timeout` | `30m` | Unload idle model after duration (`900`, `900s`, `15m`, `1h`; `0` disables) |
| `--automatic-switch` | off | Let a request for another model replace the running model; when off, a request still loads its model if none is running ([details](#automatic-switching)) |
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

- `GET /v1/models` — list available models (from the config, updated by a [reload](#reloading-the-config); a [variant model](#model-variants) appears once under its public name)
- `GET /models` — alias for `/v1/models`
- `* /{path:.*}` — proxied to the active upstream model

## How It Works

1. Client sends a request with a `model` field.
2. Switcher maps the name to the model to run (the active variant for a [variant model](#model-variants)) and checks if it is running.
3. If not (or a different model is active), it stops the current one and starts the requested model.
4. It waits for the new model to report healthy (`/health` endpoint).
5. The request is proxied to the upstream server. Request headers are forwarded except hop-by-hop headers and `Host`: the upstream gets its own address (`127.0.0.1:<port>`) as `Host`, like a default nginx proxy, so backends that reject unknown host names (such as [Strata](#strata)) work when clients use the switcher's network name.
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

Backends that print plain lines without a level field get their own rules, described in their sections: [TabbyAPI](#tabbyapi), [Strata](#strata), [TensorFold](#tensorfold), and [Splash](#splash).

Colors auto-disable when output is piped or `NO_COLOR` is set.

## Shutdown

Send `SIGINT` or `SIGTERM` to the switcher. On the first signal, it runs the optional shutdown hook, then gracefully stops the active upstream model process group (SIGTERM, then the model's [stop grace](#stop-grace-period) of 3 s by default, then SIGKILL), including servers launched through wrapper scripts. A second `SIGINT` or `SIGTERM` cancels the main server task; owned cleanup can still delay exit. This signal policy is unchanged and does not guarantee immediate forced exit. SIGKILL, power/network failure, and forced service termination cannot guarantee hooks complete. Cleanup ownership is in memory only, with no persistent recovery after restart.
