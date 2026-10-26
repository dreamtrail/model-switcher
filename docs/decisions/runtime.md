# Backend and Runtime Decisions

Complete recent backend, proxy, lifecycle, and configuration decision records, in their original relative order. Earlier definitions remain in the archive.

Return to the [decision index](../DECISIONS.md), or read the [earlier architecture and switching decisions](archive.md).

## P-20260927-01 — TabbyAPI Compatibility

### Backend Integration
- **Reuse the generic foreground-command contract** — TabbyAPI (theroyallab/tabbyAPI, ExLlamaV3) at master commit `816c321` accepts `--host`, `--port`, `--model-dir`, `--model-name`, and `--disable-auth` on the command line (overriding `config.yml`), opens its port only after the startup model has loaded, reports readiness on a public `GET /health`, serves OpenAI-compatible HTTP/SSE, and stops on SIGTERM. No backend adapter, dependency, registry, config field, alias, or request-body rewrite is needed.
- **Launch `main.py` from the TabbyAPI folder** — `config.yml`, `api_tokens.yml`, and `logs/` are relative to the working directory and commands run without a shell, so the recipe uses `bash -c "cd <tabbyAPI> && exec venv/bin/python main.py ..."`. `start.sh` / `start.py` may install packages or prompt, and `--config` drops the other command-line flags (including the assigned port), so neither is used. `TABBY_LOG_CONSOLE_WIDTH` stops 80-column wrapping when output is piped; multi-line messages such as tracebacks still emit indented continuation lines, logged as info.
- **Config key equals the model folder name** — TabbyAPI names the loaded model after the `--model-name` folder and ignores the request `model` while inline model loading is off (the default, which stays off). Model IDs are forwarded unchanged.

### Two Small Generic Changes
- **Recognize TabbyAPI log levels** — loguru/rich lines look like `HH:MM:SS.mmm LEVEL:    message` (time prefix optional with `--log-timestamps false`). A case-sensitive pattern for `TRACE|DEBUG|INFO|SUCCESS|WARNING|ERROR|CRITICAL` followed by `:` runs after the NInfer pattern and before the generic one: WARNING maps to warning, ERROR/CRITICAL to error, the rest to info. The generic pattern did not accept the time prefix or know `SUCCESS`/`DEBUG`/`TRACE`; existing llama.cpp, vLLM, and NInfer classifications are unchanged.
- **Prefill progress is not the first token** — with `return_progress` (llama.cpp extension, also in TabbyAPI), prefill emits chunks with an empty delta or text and a top-level `prompt_progress`. Such an event, when it carries no generated output, no longer marks the first token, so time to first token and live busy time start at the first generated text. Usage on any event is still applied, and every other event keeps its existing first-token rule.

### Authentication
- **Keep auth off in the example** — user decision 2026-09-27: TabbyAPI has no flag or environment variable for a pre-set key (keys come only from `api_tokens.yml`), so the recipe uses `--disable-auth true`. Every caller that reaches the switcher then acts as a TabbyAPI admin; README states this risk.
- **Document the key-file option** — users who want auth drop the flag and write their own `api_key` / `admin_key` into `api_tokens.yml`. Clients send `x-api-key` or `Authorization: Bearer`, which the switcher already forwards unchanged; `/health` needs no key, so readiness is unaffected.

### Verification Trade-off
- **Use deterministic loopback fakes pinned to `816c321`** — tests shape readiness, `\r\n` SSE framing, `: ping` comments, the non-empty-choices usage chunk with timings, prefill progress chunks, log lines, and auth headers from the pinned sources and run the real config, health, switcher, process, and proxy paths without installing TabbyAPI, downloading models, or using a GPU. README documents a manual smoke check on real hardware.

## P-20260929-06 — Client disconnect handling

### Why
- The client path runs through three userspace relays; each one ACKs and buffers on its own, so a frozen client stays invisible to the switcher until every buffer fills (or never). A write deadline or `TCP_USER_TIMEOUT` cannot detect it, and a duration cap was declined because long operations would be cut. No new flag, deadline, ping, or cap.

### What
- **Clean stop** — a client write that fails (`OSError`, incl. aiohttp "Cannot write to closing transport" / "Connection lost") ends the stream loop at once; the response is never written again (no SSE error frame, no `write_eof`). Leaving the upstream `async with` closes the upstream request, so the engine sees the disconnect. One warning line: `Client disconnected during stream (N bytes sent)`.
- **No replacement 502 after `prepare()`** — the prepared stream is always returned; the `(pre-prepare)` log label and 502 JSON happen only before `prepare()`.
- **Upstream failures unchanged** — upstream read errors still log `Stream proxy error`, set `stream_error`, and send the SSE error frame; if that frame cannot be written, it counts as a client disconnect and is suppressed. Other `write_eof` connection errors are suppressed.
- **Own error kind** — `client_disconnect` (checked before `stream_error`), per model and global.

## P-20261005-01 — Strata Compatibility

### Backend Integration
- **Reuse the generic foreground-command contract** — Strata (Niko1221/Strata) at commit `6f32ec0` runs a stdlib Python HTTP front end, `serve/server.py`, over a C++/CUDA engine. It takes `--port`, binds `127.0.0.1` by default, opens its port only after the model has loaded, and stops on SIGTERM. No backend adapter, dependency, registry, config field, or request-body rewrite is added.
- **Launch from the repo root with `--engine strata`** — the default engine is `mock` (fake replies), and Strata's own `run-<model>.sh` starts from the repo root with a relative `--config`. Commands run without a shell, so the recipe uses `env -C <Strata> python serve/server.py --engine strata --config strata-<model>.json --port ${PORT}`.
- **Any model name** — Strata serves one model and answers any request `model`; the switcher's own routing picks the model, and model IDs are forwarded unchanged.
- **Leave Strata's lazy load and idle unload off** — they duplicate the switcher's job and make `/health` report `"loaded": false`.

### Host Rewrite
- **Problem** — without an API key Strata answers 403 to a `Host` header that is a DNS name other than `localhost` (DNS-rebinding guard). The switcher forwarded the client's `Host`, so a client using `http://dgx1:8080` was refused.
- **Choice** — user decision 2026-10-05: drop `Host` from forwarded request headers for every backend, so aiohttp sends the upstream address (`127.0.0.1:<port>`), the same as nginx's default `proxy_pass`. The alternative, setting `STRATA_ALLOWED_HOSTS=*` in the recipe, was not chosen: it needs a per-backend workaround and turns the guard off.
- **Impact** — backends no longer see the client's host name. No switcher code used the forwarded `Host`; other request headers (auth, content type, custom) are still forwarded unchanged.

### Readiness and Output
- **`"loaded": false` is still loading** — a 200 `/health` JSON object with `"loaded": false` keeps the poll going regardless of `status`; `true`, an absent key, or a non-boolean value keeps the generic HTTP 200 readiness behavior.
- **Stream and stats need no change** — Strata ignores `stream_options` and always puts usage (with `cached_tokens`) and llama.cpp-style `timings` on the final chunk that also has `finish_reason`; the existing parsers read both, and `null` `*_per_second` values are ignored.
- **Log levels** — Strata prints plain lines starting `[strata] ` with no level field. For those lines only, an upper-case `WARNING`/`WARN` word means warning and `ERROR`/`FATAL`/`CRITICAL` means error (error wins if both appear); the rest is info. Lower-case "error" in message text stays info. Other lines keep the existing NInfer, TabbyAPI, and generic rules.
- **Stop grace not changed** — Strata may need more than the 3 s grace to free memory cleanly before SIGKILL. A per-model grace period is out of scope; the `shutdown_cmd` hook can stop it more slowly.

### Verification Trade-off
- **Use deterministic loopback fakes pinned to `6f32ec0`** — a stdlib HTTP/1.0 fake launched through the documented `env -C` recipe covers port-after-load, `/health`, the Host guard, JSON and SSE (`: keep-alive`, `reasoning_content`, final usage + `timings`, `[DONE]`), the `/v1/completions` 404, and `[strata] ` log lines, through the real config, health, switcher, process, and proxy paths without installing Strata, downloading models, or using a GPU. Real-hardware behavior (load time, memory release on stop) is not verified; README documents a manual smoke check.

## P-20261005-04 — TensorFold Compatibility

### Backend Integration
- **Reuse the generic foreground-command contract** — TensorFold (ashhart/TensorFold) at commit `609ca419` (release 0.6.5) runs `tensorfold serve MODEL --host H --port N --name ID`, opens its port only after the model has loaded, answers `/health` with 200 `{"status": "ok", ...}`, and stops on SIGTERM. No backend adapter, dependency, registry, config field, or health/proxy change is added; the `model` field is not enforced, so model IDs are forwarded unchanged.
- **Recipe uses `bash -c "PYTHONUNBUFFERED=1 exec tensorfold serve ..."`** — TensorFold prints request lines without flushing, so on a pipe they would arrive late. The switcher runs commands without a shell, so setting the variable needs a wrapper (`env PYTHONUNBUFFERED=1 tensorfold serve ...` works too); `exec` keeps one process, so SIGTERM reaches TensorFold as the switcher's direct child. `--no-update-check` skips the startup network check. Two-machine `--tp 2` does not fit the one-process model and is not supported.

### Prompt and Generation Speed
- **Prompt time from the `tensorfold` object** — TensorFold sends no llama.cpp `timings`. A top-level `tensorfold` object on the non-stream body and on the stream's finish chunk (usage follows in its own chunk with `include_usage`) carries `prefill_seconds` (MLX) or `prefill_s` (CUDA), both in seconds. It fills the same slot as TabbyAPI's `usage.prompt_time`, with the same rules: tokens are `prompt − cached`, an unknown cached count follows the `cache_learned` rule, and `timings.prompt_n/prompt_ms` still wins. Zero, negative, non-number, and `null` values are ignored; `prefill_seconds` is tried before `prefill_s`. On MLX the time starts at submission, so it includes TensorFold's own queue wait under concurrent load.
- **No server decode time** — `tokens_per_second` and `seconds` are not used: their exact span (queue time included or not) is unverified. Generation speed uses the switcher's own stream timing; non-streaming generation is not timed.

### Logs and Stopping
- **Log levels by the message's first word** — TensorFold prints plain lines starting `[tensorfold] ` with no level field. For those lines only, a message that starts with `warning:` means warning and one that starts with `error:`, `request error:`, or `stream error:` means error (any case); the rest is info. Later words do not count, so `... failed:` lines stay info. `stream error:` (MLX failed stream, TensorFold `server/http.py:433`) was added by user directive (P-20261006-01). Other lines keep the existing Strata, NInfer, TabbyAPI, and generic rules.
- **Stop grace not changed** — on MLX, TensorFold saves conversation checkpoints on stop (its own timeout is up to 120 s), so the 3 s grace may SIGKILL it first. Only cached checkpoints are lost, not replies. A per-model grace period is out of scope (shared follow-up with Strata, P-20261005-02); the `shutdown_cmd` hook can stop it more slowly.

### Verification Trade-off
- **Use a deterministic loopback fake pinned to `609ca419`** — a stdlib HTTP/1.1 fake installed as `tensorfold` on `PATH` and launched through the unmodified `config.example.yaml` recipe covers port-after-load, `/health`, JSON and SSE (finish chunk with `tensorfold`, separate usage chunk, `[DONE]`), a 500 with `request error:` and a stderr traceback, `PYTHONUNBUFFERED` reaching the server, log levels, and a clean SIGTERM stop, through the real config, health, switcher, process, and proxy paths. TensorFold is not installed, no model is downloaded, and no GPU is used; real-hardware behavior (load and download time, stop time, CUDA stats keys) is not verified, and README documents a manual check.

## P-20261013-01 — Model variants

### Config Shape
- **One entry, several launch variants** — user decision 2026-10-13 (option A): a top-level entry may have `variants:` (name -> command string or mapping with `cmd`, `shutdown_cmd`, `shutdown_timeout`) and an optional `default` (first variant if omitted). Plain entries are unchanged, and automatic switching between different names is kept.
- **Each variant is its own model internally** — `load_config` expands a variant to a `ModelConfig` with the id `name@variant`, so it has its own command, port, and shutdown hook (user decision: hooks per variant, because variants may use different servers). `Config.variants` holds, per public name, the variant names, `default`, and the runtime `active` choice. Everything below the entry points (quick switch, idle release, swaps, hooks, stats) works on the id unchanged.
- **Strict validation for the new syntax** — `@` is rejected in model and variant names so ids cannot collide; `variants` must be a non-empty mapping; `default` must name a variant; `cmd`/`shutdown_*` or unknown keys beside `variants`, and unknown keys inside a variant mapping, are errors. Plain mappings still ignore unknown keys, as before.

### Routing
- **Resolve once at the entry points** — `Config.resolve` maps a public name to the active variant id in `handle_request` (before the 404 check) and in `control_load_model`. Internal ids are not public: a client sending `name@variant` gets 404, and that rejection is counted only globally so it does not add an error to the real variant's stats row. `/v1/models` lists public names only. The request body is forwarded unchanged, so each variant's server must serve the public name.
- **Choice and admission in one synchronous step** — `PUT /api/v1/control/model` with `{"model", "variant"}` sets the active variant inside the admission step of `_acquire_model` (an `on_admit` callback with no await before it). Requests that arrive afterwards resolve to the new variant and join its load. If admission refuses the load (`switch_in_progress`, `queue_full`, `control_busy`, `shutting_down`), the choice does not change, so a refused switch leaves routing as it was. If the load is admitted and then fails, the choice stays: it is what the user picked. New error kinds: 404 `unknown_variant`, 400 `model_has_no_variants`.
- **State** — `models` stays the public names; `active_model`/`pending_model` are ids (they match stats rows); new key `variants`. The control page shows one button per variant with "Loaded" and "Active" badges.

### Persistence and Stats
- **In memory only** — the chosen variant is not saved; a restart starts on `default`, matching the other process-local control settings. Persisting it is open (user undecided): TODO P-20261013-02.
- **One stats row per variant** — user decision: rows are keyed by id (`qwen3.8-27b@vision`); no stats code changed.

### Script Temp Copies
- **Temp copy name includes the model id** — the copy of a startup script was named `{script}-{pid}`, so two entries using the same script overwrote each other's copy (wrong port). Variants make sharing a script likely, so the name is now `{script}-{sanitized id}-{hash of id}-{pid}`; the hash keeps ids that sanitize to the same text apart.

### Verification Trade-off
- **Fake and real local servers, no real backends** — routing, control API, atomicity, and stats are tested through the real Swapper with in-process upstreams and faked process start/stop (`tests/test_variants_routing.py`); one test runs real processes from a shared script with per-variant hooks through the real config, port rewrite, process, health, proxy, and control paths (`tests/test_variants_e2e.py`). Real model servers were not run.

## P-20261013-04 — Reload config without restart

### Trigger
- **Control API and panel button first** — user decision 2026-10-13: `POST /api/v1/control/reload` (no body, needs `X-Model-Switcher-Control: 1`) and a Reload config button on `/control`. No file watching. SIGHUP was left unhandled at first and is handled since P-20261013-07.
- **Same file as startup** — the CLI keeps the absolute `--config` path on the swapper (set after construction so test fakes keep their constructor). A swapper without a path answers `config_invalid`.

### What Changes
- **Compare raw specs, keep unchanged entries** — `ModelConfig.spec` (new last field) holds the config text of `cmd`, `shutdown_cmd`, and `shutdown_timeout` before port rewriting. An entry whose spec is the same keeps its old `ModelConfig` object: same port, rewritten command, and temp script. Entries built in code have no spec and always count as changed. Script file contents are added to the comparison by P-20261013-07.
- **New and changed entries get fresh ports and temp copies** — ports are picked clear of kept entries, of each other, and of the running process. Temp copies made by a reload carry an `r<N>` generation in the name, so a copy that a running process may still read is never overwritten. Old copies stay until shutdown removes the temp dir.
- **Variant choice carried over** — the active variant stays if it still exists, else the entry's `default`. The switcher does not record whether the active variant was picked or only the default, so a changed `default:` does not move it (review m2; kept as is, documented in README). Runtime settings are not in the YAML and are not touched.
- **One synchronous swap** — the reload is a sync method: parse, build, then assign `self.config` with no await in between. It is refused with 409 `control_busy` while `_swapping` or a stop-only operation (unload, idle) is in progress, and with 503 `shutting_down` during shutdown. A missing or invalid file (including YAML syntax errors, non-string `cmd` values, and unreadable scripts) keeps the old config and returns 400 `config_invalid` with a one-line message of at most 300 characters.
- **Error messages hide paths and file text** — the panel must not show the config path or model commands, so reload errors name only the config file's base name, and YAML errors keep the problem and line/column but drop PyYAML's quoted source snippet (review m3). Startup errors on the console keep the full text.
- **`cmd` must be a string** — `load_config` now rejects a non-string `cmd` (for example `cmd: 123` or a list) with a `ValueError`; before, it raised `TypeError`/`AttributeError`, which gave HTTP 500 on reload and a traceback at startup (review M1). A blank `cmd:` is still "empty command".

### Running Model
- **Keep it running unchanged** — user decision 2026-10-13. The process uses its handle (port, hook copied at spawn), not the config, so a changed entry keeps its old settings until its next start; a removed entry keeps serving model-less and in-flight requests until a switch, unload, or idle release, while named requests get 404. The summary flag `running_changed` tells the panel to show a note.
- **No config lookups after awaits** — `_ensure_model` resolves the `ModelConfig` once and passes it to `_start_tracked`, which no longer looks it up again after the previous model's stop. A reload cannot run during a swap today, so this is a defensive fix: before it, a lookup that failed after an await would raise outside `_acquire_model`'s handled exceptions and leave `_swapping` stuck.

### Stats and Persistence
- **Stats untouched** — removed models keep their history rows. The YAML file is only read, never written.

### Verification Trade-off
- **Fake and real local servers** — refusals, change detection, ports, temp copies, variants, and the API are tested with faked process start/stop (`tests/test_config_reload.py`, `tests/test_config_reload_scripts.py`); one test runs real servers from a shared script through add, change, remove, and invalid reloads (`tests/test_config_reload_e2e.py`). The panel script is run in Node with a fake DOM. Real model servers and a real browser were not used.

## P-20261013-07 — Variant and reload minors, SIGHUP reload

User approved the plan 2026-10-13 (IDs P-20261013-03, -06, -05, in that order).

### Config Validation
- **Names must be strings** — `load_config` rejects a model or variant name that YAML read as a number, boolean, or null (`123:`, `yes:`, `~:`) with a `ValueError` that says to put quotes around the name. Before, a numeric name loaded and then broke the reload log line and temp script name (HTTP 500). The same error is now a 400 `config_invalid` on reload and a one-line `Config error` at startup. The message shows the value with its type (`123 (int)`, `2024-01-01 (date)`, `null`), and the `default` mismatch message quotes strings, so `default: 1` against a variant `'1'` reads `default 1 (int) is not one of its variants: '1'`.
- **`default` needs `variants`** — an entry with `default` but no `variants` (a typo such as `variant:`) is an error instead of a plain entry that silently ignores `default`. Other unknown keys in a plain mapping are still ignored, as before.

### Reload
- **Script contents are part of the change check** — `ModelConfig.spec` has a fourth item: a SHA-256 of the script file when the first token of `cmd` is an existing file (`port_rewrite.script_fingerprint`), else None. The check does not look at `${PORT}`, because the loader fills it in before the startup copy step, so such an entry is a script copy too. To give a changed or new script entry the same result after a reload, the reload now fills in `${PORT}` with the new port before the copy step (only when the first token is a file; other commands keep the raw `${PORT}`, which is substituted later). Before, a reload kept the raw placeholder and ran the original script in place. Editing a script with the same config line now counts as `changed`: new port, new temp copy, used by the next start; the running process keeps its old copy. A script that cannot be read gets a marker hash, so loading still works; the entry then counts as changed and the copy step returns `config_invalid` (old config kept). This replaces the earlier "script edits need a restart" limit. A script used only through `shutdown_cmd` is not tracked.
- **Reload counter moves on success only** — the `r<N>` generation in temp copy names advances when the new config is assigned. A failed attempt may leave unused copies; the next attempt reuses the name and overwrites them (they were never in use).
- **Port test uses fixed ports** — the running model and the kept model get distinct fixed ports, so the test fails if either reservation is missing (checked in a temp copy: without the running-port reservation it fails).

### SIGHUP
- **SIGHUP runs the same reload as the API** — `src/cli.py` registers SIGHUP next to SIGINT/SIGTERM (skipped where `add_signal_handler` or `SIGHUP` is unavailable) and calls `Swapper.control_reload_config`. Success is logged at info with counts, an invalid config at warning with the reason, `control_busy` at warning ("send SIGHUP again"), and shutdown at info. A busy reload is not retried or queued: a retry loop would hide that a model is loading, and the user can send the signal again. Any unexpected error is logged and swallowed. Once the handler is registered, SIGHUP never exits the switcher, and after a shutdown signal it is ignored. A SIGHUP that arrives earlier (config load and swapper setup, before `Listening on`) still terminates the process, because the handler is registered with the other signals just before the server starts; moving it earlier was not worth the risk of reloading a half-built swapper.
- **systemd** — `ExecReload=/bin/kill -HUP $MAINPID` in the unit shown in `docs/SETUP.md`; the wrapper must `exec` the switcher so `$MAINPID` is the switcher. A bash wrapper without `exec` is killed by SIGHUP, so systemd sees the service exit (restart under `Restart=on-failure`: inferred, not tested).
- **No new trigger beyond SIGHUP** — file watching is still not added.

### Verification Trade-off
- **Real process for SIGHUP** — `tests/test_cli_sighup_e2e.py` starts `python -m src` with a real shell-script model server, edits the config, sends SIGHUP (valid, invalid, valid again), and ends with SIGTERM. Unit tests in `tests/test_cli.py` cover the handler wiring with a fake swapper. No systemd run was done.

## P-20261013-10 — Programs given by path

User approved the plan 2026-10-13. Fixes TODO P-20261013-08.

### Script Detection
- **Script mode needs a text file** — a command whose first word is an existing file used to count as a script, so `/opt/llama/llama-server --model m.gguf` made startup crash with `UnicodeDecodeError` (and a reload answer `config_invalid`). `port_rewrite._is_text_script` is now the one check behind `rewrite_command`, `is_script_command`, and `script_fingerprint`. Text means valid UTF-8 with no NUL bytes. Any other file is a program: its port flags are replaced or `--port <N>` is appended, with no temp copy, and its fingerprint is None. Commands on `PATH` were never affected.
- **Chunked read, early exit** — the file is read in 64 KiB chunks and the check stops at the first NUL byte or bad UTF-8, so a large binary is not read in full. An empty file counts as text.
- **Unreadable stays a script** — a file that cannot be opened (permissions) still counts as a script, so the copy step reports `Permission denied` as before (`config_invalid` on reload).
- **Edits that change the kind** — turning a script into a binary (or back) changes its fingerprint, so a reload marks the entry `changed`. Editing a binary alone is not tracked, like any program on `PATH`.
- **Existing tests changed** — three reload tests used a non-UTF-8 file as the "unreadable script" or "unreadable content"; that file is now a program, so they use a mode 0 text file instead (skipped when the user can still read it, such as root).

### Out of Scope
- **Doubled port flag** — a `${PORT}` command with a `--port` flag still gets the flag twice at startup (P-20261013-09); not changed here.

### Verification Trade-off
- **Real interpreter as the binary** — `tests/test_config_reload_e2e.py` starts a real HTTP server with `sys.executable` given by absolute path, calls it, and reloads. The unit tests use small fake binaries (NUL bytes, invalid UTF-8, bad bytes after the first chunk). Real model servers such as `llama-server` were not run.
