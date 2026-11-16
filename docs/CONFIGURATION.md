# Configuration and backend recipes

How to write the YAML config that maps model names to launch commands, and recipes for each supported backend server: [SGLang](#sglang), [NInfer](#ninfer), [TabbyAPI](#tabbyapi), [Strata](#strata), [TensorFold](#tensorfold), and [Splash](#splash). When a request may load or replace a model is covered in [switching and the control panel](CONTROL.md).

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
- After editing the file, apply it without a restart: see [Reloading the config](CONTROL.md#reloading-the-config).

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
[service stop budgets and limitations](SETUP.md#shutdown-hook-stop-budgets).

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
model keeps the value it started with; a [reload](CONTROL.md#reloading-the-config)
applies a new value at its next start. The recipes for [Strata](#strata)
(`stop_grace: 25`), [TensorFold](#tensorfold) on MLX (`stop_grace: 130`), and
[Splash](#splash) (`stop_grace: 20`) set it; each section explains its value.

### Model variants

A variant entry gives one model name several launch commands. Clients (for
example an agent harness) always send the same name; you pick which variant
runs from the [control panel or API](CONTROL.md#control-panel-and-runtime-api). The
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
  `pending_model`, logs, and [statistics](STATISTICS.md#statistics) rows (one row per variant).
  Clients cannot use it: a request for `qwen3.8-27b@vision` gets 404.
- A request for `qwen3.8-27b` goes to the active variant, which starts as
  `default`. Switching the variant stops the running one (its own hook first),
  starts the new one, and sends later `qwen3.8-27b` requests to it.
- The request body is forwarded unchanged, so every variant's server must
  accept the public name (`-a`, `--served-model-name`, or the like).
- `GET /v1/models` lists `qwen3.8-27b` once, not the variants.
- The chosen variant is kept in memory only. A [config reload](CONTROL.md#reloading-the-config)
  keeps it (unless that variant was removed), even if you changed `default`;
  a restart starts each entry on its `default`.

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

**Request guards.** Without an API key, Strata refuses (403) any request whose `Host` is a DNS name other than `localhost`, to guard against DNS rebinding. The switcher already replaces `Host` with `127.0.0.1:<port>` ([How It Works](ARCHITECTURE.md#how-it-works)), so clients can use any name, for example `http://dgx1:8080`, and you do not need `STRATA_ALLOWED_HOSTS`.

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

**Stopping.** On a swap or shutdown, model-switcher sends SIGTERM to the process group. TensorFold stops cleanly on SIGTERM, but on MLX it first saves recent conversation checkpoints to disk, which may take longer than the default 3 s wait (TensorFold allows up to 120 s; real times not measured). `stop_grace: 130` lets it finish ([stop grace period](#stop-grace-period)); the wait ends as soon as TensorFold exits, so a quick stop costs no extra time. A slow save delays the next model's start by that long, so you may prefer a lower value or none: a SIGKILL loses only those checkpoints, which are a cache, and replies already sent are not affected. The value is for MLX; how long a CUDA stop takes was not checked. Under systemd, 130 s is longer than the default 90 s stop timeout, so raise `TimeoutStopSec` to cover it (see [SETUP](SETUP.md)).

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

**Request guards.** Splash refuses (403) a request whose `Host` is not its bind address, `localhost`, `127.0.0.1`, or `::1`. The switcher sends `127.0.0.1:<port>` as `Host` ([How It Works](ARCHITECTURE.md#how-it-works)), so clients on the LAN can use the switcher's network name (for example `http://mac:8080`). A browser `Origin` header from a site Splash does not allow also gets 403. The switcher passes `Origin` through unchanged, so a web page on another site needs Splash's `--allowed-origin`.

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
