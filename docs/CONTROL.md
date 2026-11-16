# Switching and the control panel

When a request may load or replace a running model, and how to inspect and change the running switcher from the browser panel at `/control` or its HTTP API, including config reload without a restart. The config file format is in [CONFIGURATION](CONFIGURATION.md).

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

Open `http://localhost:8080/control` to use the dependency-free control panel on the existing listener. It shows configured, active, and pending models, lifecycle state, in-flight request count, and the three runtime settings. It can load or switch to a model, unload the active model, update settings, and [reload the config file](#reloading-the-config). A [variant model](CONFIGURATION.md#model-variants) shows its name with one button per variant: the loaded variant has a "Loaded" badge, and the active variant (where requests go) has an "Active" badge when it is not loaded; clicking a variant makes it active and loads it. It uses the same look as the statistics dashboard (light and dark theme, status tiles, cards); the header links to `/stats` (the dashboard links back to `/control`). It never displays or edits model commands, assigned ports, process IDs, environment values, or the config path.

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

`models` lists the names clients send. `variants` has one entry per [variant model](CONFIGURATION.md#model-variants) (empty `{}` when there are none). `active_model` and `pending_model` are model ids, so a variant shows as `qwen3.8-27b@text`.

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

**SIGHUP** does the same reload: `kill -HUP <pid>` re-reads the file and logs `SIGHUP: config reloaded (added 1, removed 0, changed 1)`, or a warning with the reason when it did not reload (invalid file, or a model is loading or unloading: send it again later). Once the switcher is running it never exits on SIGHUP. During startup (before it logs `Listening on`), SIGHUP still terminates it, as it did before. For systemd, add `ExecReload=/bin/kill -HUP $MAINPID` so `systemctl reload` works; the wrapper must `exec` the switcher ([unit and wrapper](SETUP.md#reload-the-config-with-systemctl)). There is no file-watch trigger.
