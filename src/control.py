"""Same-listener runtime control HTTP API and browser panel."""

from __future__ import annotations

import json
import math
from typing import Protocol

from aiohttp import web

CONTROL_HEADER = "X-Model-Switcher-Control"
CONTROL_HEADER_VALUE = "1"
MAX_CONTROL_BODY = 4096


class ControlController(Protocol):
    """Operations exposed to the HTTP control adapter."""

    def control_state(self) -> dict:
        """Return one coherent process-local state snapshot."""

    def control_apply_settings(
        self,
        *,
        automatic_switch: bool,
        quick_switch: bool,
        idle_timeout_seconds: float,
    ) -> str:
        """Atomically apply settings and return a lifecycle outcome kind."""

    async def control_load_model(self, model_name: str) -> str:
        """Load a configured model and return a lifecycle outcome kind."""

    async def control_unload_model(self) -> str:
        """Unload the tracked model and return a lifecycle outcome kind."""


_METHODS = {
    "/control": ("GET",),
    "/api/v1/control/state": ("GET",),
    "/api/v1/control/settings": ("PUT",),
    "/api/v1/control/model": ("PUT", "DELETE"),
}

_OUTCOME_ERRORS = {
    "switch_in_progress": (
        409,
        "A different model is currently loading",
        "switch_in_progress",
    ),
    "swap_superseded": (
        409,
        "Model load was superseded by a newer request",
        "swap_superseded",
    ),
    "model_start_failed": (
        500,
        "Model process could not be started",
        "model_start_failed",
    ),
    "process_cleanup_failed": (
        503,
        "Previous model process could not be stopped",
        "process_cleanup_failed",
    ),
    "shutting_down": (
        503,
        "Model switcher is shutting down",
        "shutting_down",
    ),
    "queue_full": (
        503,
        "Swap in progress, try again shortly",
        None,
    ),
    "swap_timeout": (504, "Model swap timed out", "swap_timeout"),
    "control_busy": (
        409,
        "A model lifecycle operation is already in progress",
        "control_busy",
    ),
    "automatic_switch_disabled": (
        409,
        "Automatic model switching is disabled",
        "automatic_switch_disabled",
    ),
}


def _error_payload(message: str, code: str | None) -> dict:
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": None,
            "code": code,
        }
    }


def _error(status: int, message: str, code: str | None) -> web.Response:
    return web.json_response(_error_payload(message, code), status=status)


def _outcome_error(kind: str) -> web.Response:
    status, message, code = _OUTCOME_ERRORS[kind]
    return _error(status, message, code)


def _state_response(controller: ControlController) -> web.Response:
    return web.json_response(
        controller.control_state(),
        headers={"Cache-Control": "no-store"},
    )


async def _read_limited(request: web.Request, limit: int) -> bytes | None:
    """Read at most limit bytes, returning None if the request is larger."""
    if request.content_length is not None and request.content_length > limit:
        return None
    body = bytearray()
    while len(body) <= limit:
        chunk = await request.content.read(limit + 1 - len(body))
        if not chunk:
            return bytes(body)
        body.extend(chunk)
    return None


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


async def _read_json(request: web.Request) -> tuple[object | None, web.Response | None]:
    if request.content_type != "application/json":
        return None, _error(
            415,
            "Content-Type must be application/json",
            "unsupported_media_type",
        )
    body = await _read_limited(request, MAX_CONTROL_BODY)
    if body is None:
        return None, _error(
            413,
            "Control request body is too large",
            "control_request_too_large",
        )
    try:
        return json.loads(
            body,
            parse_constant=_reject_json_constant,
            object_pairs_hook=_unique_json_object,
        ), None
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, _error(
            400,
            "Invalid control request",
            "invalid_control_request",
        )


def _valid_idle_timeout(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except (OverflowError, TypeError):
        return False


def _mutation_forbidden(request: web.Request) -> web.Response | None:
    if request.headers.get(CONTROL_HEADER) == CONTROL_HEADER_VALUE:
        return None
    return _error(
        403,
        "Control mutation header is required",
        "control_request_forbidden",
    )


async def _put_settings(
    request: web.Request, controller: ControlController
) -> web.Response:
    forbidden = _mutation_forbidden(request)
    if forbidden is not None:
        return forbidden
    data, error = await _read_json(request)
    if error is not None:
        return error
    expected = {
        "automatic_switch",
        "quick_switch",
        "idle_timeout_seconds",
    }
    if not isinstance(data, dict) or set(data) != expected:
        return _error(
            400, "Invalid control request", "invalid_control_request"
        )
    automatic_switch = data["automatic_switch"]
    quick_switch = data["quick_switch"]
    idle_timeout = data["idle_timeout_seconds"]
    valid_timeout = _valid_idle_timeout(idle_timeout)
    if (
        not isinstance(automatic_switch, bool)
        or not isinstance(quick_switch, bool)
        or not valid_timeout
    ):
        return _error(
            400, "Invalid control request", "invalid_control_request"
        )
    outcome = controller.control_apply_settings(
        automatic_switch=automatic_switch,
        quick_switch=quick_switch,
        idle_timeout_seconds=float(idle_timeout),
    )
    if outcome != "success":
        return _outcome_error(outcome)
    return _state_response(controller)


async def _put_model(
    request: web.Request, controller: ControlController
) -> web.Response:
    forbidden = _mutation_forbidden(request)
    if forbidden is not None:
        return forbidden
    data, error = await _read_json(request)
    if error is not None:
        return error
    if (
        not isinstance(data, dict)
        or set(data) != {"model"}
        or not isinstance(data["model"], str)
        or not data["model"]
    ):
        return _error(
            400, "Invalid control request", "invalid_control_request"
        )
    model_name = data["model"]
    outcome = await controller.control_load_model(model_name)
    if outcome == "unknown_model":
        return _error(404, f"Unknown model: {model_name}", "unknown_model")
    if outcome != "success":
        return _outcome_error(outcome)
    return _state_response(controller)


async def _delete_model(
    request: web.Request, controller: ControlController
) -> web.Response:
    forbidden = _mutation_forbidden(request)
    if forbidden is not None:
        return forbidden
    body = await _read_limited(request, 0)
    if body is None or body:
        return _error(
            400, "Invalid control request", "invalid_control_request"
        )
    outcome = await controller.control_unload_model()
    if outcome != "success":
        return _outcome_error(outcome)
    return _state_response(controller)


async def dispatch_control(
    request: web.Request, controller: ControlController
) -> web.Response | None:
    """Handle a reserved control route, or return None when not reserved."""
    path = request.path
    recognized = _METHODS.get(path)
    if recognized is None:
        if path == "/api/v1/control" or path.startswith(
            "/api/v1/control/"
        ):
            return _error(
                404, "Control route not found", "control_route_not_found"
            )
        return None

    if request.method not in recognized:
        response = _error(405, "Method not allowed", "method_not_allowed")
        response.headers["Allow"] = ", ".join(recognized)
        return response

    if path == "/control":
        return web.Response(
            text=CONTROL_HTML,
            content_type="text/html",
            charset="utf-8",
            headers={"Cache-Control": "no-store"},
        )
    if path == "/api/v1/control/state":
        return _state_response(controller)
    if path == "/api/v1/control/settings":
        return await _put_settings(request, controller)
    if request.method == "PUT":
        return await _put_model(request, controller)
    return await _delete_model(request, controller)


CONTROL_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Model Switcher — Control</title>
<style>
  /* Palette, type, and card styles copied from the /stats page (STATS_HTML
     in src/stats.py); a shared stylesheet is deferred to P-20260925-05. */
  :root {
    color-scheme: light;
    --plane: #f9f9f7;
    --surface: #fcfcfb;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --muted: #898781;
    --hairline: #e1e0d9;
    --baseline: #c3c2b7;
    --ring: rgba(11, 11, 11, 0.10);
    --wash: rgba(11, 11, 11, 0.03);
    --accent: #2a78d6;
    --on-accent: #ffffff;
    --good: #0ca30c;
    --serious: #ec835a;
    --critical: #d03b3b;
    --good-tint: rgba(12, 163, 12, 0.10);
    --critical-tint: rgba(208, 59, 59, 0.08);
    --shadow: 0 1px 2px rgba(11, 11, 11, 0.04), 0 2px 6px rgba(11, 11, 11, 0.04);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --plane: #0d0d0d;
      --surface: #1a1a19;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --hairline: #2c2c2a;
      --baseline: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --wash: rgba(255, 255, 255, 0.04);
      --accent: #3987e5;
      --good-tint: rgba(12, 163, 12, 0.18);
      --critical-tint: rgba(208, 59, 59, 0.16);
      --shadow: none;
    }
  }
  *, *::before, *::after { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html { background: var(--plane); }
  body {
    margin: 0; background: var(--plane); color: var(--ink);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    font-size: 15px; line-height: 1.45;
    -webkit-font-smoothing: antialiased;
  }
  main { max-width: 80rem; margin: 0 auto; padding: 2rem 1.5rem 2.5rem; }
  .top { margin-bottom: 1.25rem; }
  h1 { margin: 0; font-size: 1.375rem; font-weight: 650; letter-spacing: -0.01em; }
  .subtitle { margin: 0.1rem 0 0; color: var(--ink-2); font-size: 0.9rem; }
  .subtitle a, noscript a { color: inherit; text-underline-offset: 2px; }
  .subtitle a:hover { color: var(--accent); }
  .dot {
    display: inline-block; width: 0.5rem; height: 0.5rem; border-radius: 50%;
    flex: none; background: var(--muted);
  }
  .dot.good { background: var(--good); }
  .dot.serious { background: var(--serious); }
  .dot.critical { background: var(--critical); }
  .card {
    background: var(--surface); border: 1px solid var(--ring);
    border-radius: 12px; box-shadow: var(--shadow); min-width: 0;
  }
  .kpis {
    display: grid; gap: 0.75rem; margin-bottom: 1rem;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  @media (min-width: 40rem) { .kpis { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
  .tile { padding: 0.85rem 1rem 0.8rem; }
  .tile .label { color: var(--ink-2); font-size: 0.8rem; }
  .tile .value {
    margin-top: 0.3rem; font-size: 1.6rem; font-weight: 600; line-height: 1.15;
    letter-spacing: -0.015em; font-variant-numeric: proportional-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .tile .value.name {
    font-size: 1.15rem; line-height: 1.6; letter-spacing: 0;
    white-space: normal; overflow-wrap: anywhere;
  }
  .tile .sub {
    margin-top: 0.3rem; color: var(--muted); font-size: 0.78rem; line-height: 1.35;
  }
  .tile .sub .dot { margin-right: 0.35rem; vertical-align: 0.05em; }
  .section { margin-bottom: 1rem; }
  .section h2 {
    margin: 0; padding: 0.9rem 1.25rem 0.6rem;
    font-size: 0.95rem; font-weight: 600;
  }
  .body { padding: 0.1rem 1.25rem 1.1rem; }
  .models { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.9rem; }
  button {
    display: inline-flex; align-items: center; gap: 0.45rem;
    padding: 0.4rem 0.85rem; border-radius: 8px;
    border: 1px solid var(--ring); background: var(--surface); color: var(--ink);
    font: inherit; font-size: 0.875rem; cursor: pointer;
  }
  button:hover:not(:disabled) { background: var(--wash); border-color: var(--baseline); }
  button:focus-visible, input:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button:disabled { opacity: 0.55; cursor: default; }
  button.primary { background: var(--accent); border-color: var(--accent); color: var(--on-accent); }
  button.primary:hover:not(:disabled) { background: var(--accent); filter: brightness(1.08); }
  button.model.loaded { border-color: var(--good); font-weight: 600; }
  .pill {
    display: inline-flex; align-items: center; gap: 0.3rem;
    padding: 0.05rem 0.5rem 0.05rem 0.4rem; border-radius: 999px;
    background: var(--good-tint); color: var(--ink-2);
    font-size: 0.7rem; font-weight: 500;
  }
  .pill .dot { width: 0.4rem; height: 0.4rem; }
  .setting {
    display: flex; align-items: center; gap: 0.6rem; margin: 0 0 0.75rem;
    color: var(--ink-2); font-size: 0.9rem;
  }
  input[type="checkbox"] { width: 1rem; height: 1rem; margin: 0; accent-color: var(--accent); }
  input[type="number"] {
    width: 9rem; padding: 0.35rem 0.55rem; border-radius: 8px;
    border: 1px solid var(--ring); background: var(--plane); color: var(--ink);
    font: inherit; font-size: 0.875rem; font-variant-numeric: tabular-nums;
  }
  #message { min-height: 1.5rem; margin: 0; color: var(--ink-2); font-size: 0.875rem; white-space: pre-wrap; }
  #message.error {
    display: inline-block; padding: 0.25rem 0.7rem; border-radius: 8px;
    background: var(--critical-tint); color: var(--ink);
  }
  #message.error::before {
    content: ""; display: inline-block; width: 0.5rem; height: 0.5rem;
    margin-right: 0.45rem; border-radius: 50%; background: var(--critical);
    vertical-align: 0.05em;
  }
  @media (max-width: 34rem) {
    main { padding: 1.25rem 0.875rem 2rem; }
    .tile .value { font-size: 1.35rem; }
  }
</style>
</head>
<body>
<main>
<header class="top">
  <h1>Model Switcher</h1>
  <p class="subtitle">Control panel · <a href="/stats">Statistics</a></p>
</header>
<section class="kpis" aria-label="Status">
  <div class="card tile">
    <div class="label">Lifecycle</div>
    <div id="lifecycle" class="value">–</div>
    <div class="sub"><span id="lifecycle-dot" class="dot"></span><span id="lifecycle-sub">–</span></div>
  </div>
  <div class="card tile">
    <div class="label">Active model</div>
    <div id="active-model" class="value name">–</div>
    <div class="sub">serving requests</div>
  </div>
  <div class="card tile">
    <div class="label">Pending model</div>
    <div id="pending-model" class="value name">–</div>
    <div class="sub">loading next</div>
  </div>
  <div class="card tile">
    <div class="label">In-flight requests</div>
    <div id="inflight" class="value">–</div>
    <div class="sub">running now</div>
  </div>
</section>
<section class="card section">
  <h2>Models</h2>
  <div class="body">
    <div id="models" class="models"></div>
    <button id="unload" type="button">Unload active model</button>
  </div>
</section>
<section class="card section">
  <h2>Runtime settings</h2>
  <div class="body">
    <label class="setting"><input id="automatic-switch" type="checkbox"> Allow request-driven model changes</label>
    <label class="setting"><input id="quick-switch" type="checkbox"> Let a newer target supersede a pending load</label>
    <label class="setting">Idle timeout (seconds) <input id="idle-timeout" type="number" min="0" step="any"></label>
    <button id="save-settings" class="primary" type="button">Apply settings</button>
  </div>
</section>
<p id="message" role="status"></p>
<noscript><p>JavaScript is required. State JSON is available at <a href="/api/v1/control/state">/api/v1/control/state</a>.</p></noscript>
</main>
<script>
"use strict";
const API = "/api/v1/control";
const DASH = "–";
let latestState = null;
let mutating = false;

function byId(id) { return document.getElementById(id); }
function text(id, value) {
  const node = byId(id);
  if (node) node.textContent = value == null ? DASH : String(value);
}
function setMessage(value, failed) {
  const node = byId("message");
  if (!node) return;
  node.textContent = value || "";
  node.className = failed ? "error" : "";
}
function setDisabled(disabled) {
  const controls = document.querySelectorAll("button, input");
  for (const control of controls) control.disabled = disabled;
}
// Lifecycle value -> [status dot tone, plain-language sub-line].
const LIFECYCLE = {
  ready: ["good", "Serving"],
  loading: ["serious", "Loading a model"],
  unloading: ["serious", "Unloading"],
  idle: ["", "No model loaded"],
  shutting_down: ["serious", "Shutting down"],
  cleanup_failed: ["critical", "Cleanup failed"],
};
function renderLifecycle(value) {
  const known = LIFECYCLE[value] || ["", value == null ? DASH : "Unknown state"];
  const dot = byId("lifecycle-dot");
  if (dot) dot.className = known[0] ? "dot " + known[0] : "dot";
  text("lifecycle-sub", known[1]);
}
function modelButton(name, loaded) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = loaded ? "model loaded" : "model";
  const label = document.createElement("span");
  label.textContent = name;
  button.appendChild(label);
  if (loaded) {
    const pill = document.createElement("span");
    pill.className = "pill";
    const dot = document.createElement("span");
    dot.className = "dot good";
    pill.appendChild(dot);
    const word = document.createElement("span");
    word.textContent = "Loaded";
    pill.appendChild(word);
    button.appendChild(pill);
  }
  return button;
}
function render(state) {
  if (!state || typeof state !== "object") return;
  latestState = state;
  text("lifecycle", state.lifecycle);
  renderLifecycle(state.lifecycle);
  text("active-model", state.active_model);
  text("pending-model", state.pending_model);
  text("inflight", state.inflight_requests);
  const settings = state.settings || {};
  byId("automatic-switch").checked = settings.automatic_switch === true;
  byId("quick-switch").checked = settings.quick_switch === true;
  byId("idle-timeout").value = String(settings.idle_timeout_seconds ?? 0);
  const list = byId("models");
  list.textContent = "";
  for (const name of Array.isArray(state.models) ? state.models : []) {
    const button = modelButton(String(name), name === state.active_model);
    button.disabled = mutating;
    button.addEventListener("click", function () { loadModel(String(name)); });
    list.appendChild(button);
  }
  setDisabled(mutating);
}
async function decode(response) {
  let data = null;
  try { data = await response.json(); } catch (error) { /* handled below */ }
  if (!response.ok) {
    const detail = data && data.error && data.error.message;
    throw new Error(detail || ("HTTP " + response.status));
  }
  return data;
}
async function refresh() {
  try {
    const response = await fetch(API + "/state", {cache: "no-store"});
    render(await decode(response));
  } catch (error) {
    setMessage(error instanceof Error ? error.message : String(error), true);
  }
}
async function mutate(path, method, body) {
  if (mutating) return;
  mutating = true;
  setDisabled(true);
  setMessage("Working…", false);
  const options = {
    method: method,
    headers: {"X-Model-Switcher-Control": "1"},
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(API + path, options);
    render(await decode(response));
    setMessage("Updated", false);
  } catch (error) {
    setMessage(error instanceof Error ? error.message : String(error), true);
  } finally {
    mutating = false;
    setDisabled(false);
    await refresh();
  }
}
function loadModel(name) { return mutate("/model", "PUT", {model: name}); }
function unloadModel() { return mutate("/model", "DELETE"); }
function saveSettings() {
  if (!latestState) return;
  const timeout = Number(byId("idle-timeout").value);
  return mutate("/settings", "PUT", {
    automatic_switch: byId("automatic-switch").checked,
    quick_switch: byId("quick-switch").checked,
    idle_timeout_seconds: timeout,
  });
}
byId("unload").addEventListener("click", unloadModel);
byId("save-settings").addEventListener("click", saveSettings);
refresh();
setInterval(refresh, 3000);
</script>
</body>
</html>
"""
