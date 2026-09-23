"""Static-safety and executable JavaScript tests for the control panel."""

from __future__ import annotations

import ast
import json
from pathlib import Path
import re
import shutil
import subprocess

from aiohttp.test_utils import TestClient
import pytest

from src.control import CONTROL_HTML
from tests.stats_helpers import _stack


FRESH = {
    "models": ["a", "<img src=x onerror=alert(1)>"] ,
    "active_model": None,
    "pending_model": None,
    "lifecycle": "idle",
    "inflight_requests": 0,
    "settings": {
        "automatic_switch": True,
        "quick_switch": False,
        "idle_timeout_seconds": 10,
    },
}


class TestPanelHttpAndStaticSafety:
    async def test_panel_is_utf8_no_store_and_contains_every_control(
        self, monkeypatch
    ):
        async with _stack(monkeypatch, extra_models=("a",)) as stack:
            async with TestClient(stack.proxy) as client:
                response = await client.get("/control")
                body = await response.text()

        assert response.status == 200
        assert response.content_type == "text/html"
        assert response.charset.lower() == "utf-8"
        assert response.headers["Cache-Control"] == "no-store"
        for required in (
            'id="lifecycle"',
            'id="active-model"',
            'id="pending-model"',
            'id="inflight"',
            'id="models"',
            'id="unload"',
            'id="automatic-switch"',
            'id="quick-switch"',
            'id="idle-timeout"',
            'id="save-settings"',
            'API + "/state"',
            "setInterval(refresh, 3000)",
        ):
            assert required in body

    def test_panel_uses_text_only_same_origin_rendering_without_browser_storage(self):
        assert ".innerHTML" not in CONTROL_HTML
        assert ".textContent" in CONTROL_HTML
        assert 'const API = "/api/v1/control"' in CONTROL_HTML
        assert '"X-Model-Switcher-Control": "1"' in CONTROL_HTML
        assert 'options.headers["Content-Type"] = "application/json"' in CONTROL_HTML
        assert "credentials" not in CONTROL_HTML
        assert "localStorage" not in CONTROL_HTML
        assert "sessionStorage" not in CONTROL_HTML
        assert "document.cookie" not in CONTROL_HTML
        assert "cookie" not in CONTROL_HTML.lower()
        assert "innerHTML" not in CONTROL_HTML
        assert not re.search(r'<(?:script|link|img)[^>]+(?:src|href)=["\']https?://', CONTROL_HTML)
        assert "{{" not in CONTROL_HTML and "${" not in CONTROL_HTML

    def test_control_module_does_not_import_config_process_or_new_dependencies(self):
        source = (Path(__file__).resolve().parents[1] / "src" / "control.py").read_text()
        tree = ast.parse(source)
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        assert roots <= {"__future__", "json", "math", "typing", "aiohttp"}
        for forbidden in ("process_manager", "src.config", "subprocess"):
            assert forbidden not in source


class TestExecutablePanelJavaScript:
    @staticmethod
    def _execute(expression: str, responses: list[dict] | None = None):
        node = shutil.which("node")
        if node is None:
            pytest.fail("Node.js is required to execute the embedded control panel JavaScript")
        script = CONTROL_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
        response_json = json.dumps(responses or [{"ok": True, "status": 200, "data": FRESH}])
        harness = r'''
class Element {
  constructor(tagName, id = null) {
    this.tagName = tagName;
    this.id = id;
    this.children = [];
    this._textContent = "";
    this.value = "";
    this.checked = false;
    this.disabled = false;
    this.className = "";
    this.type = "";
    this.listeners = new Map();
  }
  set textContent(value) {
    this._textContent = String(value);
    if (value === "") this.children = [];
  }
  get textContent() {
    return this.children.length
      ? this.children.map((child) => child.textContent).join("")
      : this._textContent;
  }
  appendChild(child) { this.children.push(child); return child; }
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  click() {
    const callback = this.listeners.get("click");
    return callback ? callback({target: this}) : undefined;
  }
}
const ids = [
  "lifecycle", "active-model", "pending-model", "inflight", "models",
  "unload", "automatic-switch", "quick-switch", "idle-timeout",
  "save-settings", "message"
];
const elements = new Map(ids.map((id) => [id, new Element("div", id)]));
const fixedControls = [
  elements.get("unload"), elements.get("automatic-switch"),
  elements.get("quick-switch"), elements.get("idle-timeout"),
  elements.get("save-settings")
];
const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement(tagName) { return new Element(tagName); },
  querySelectorAll(selector) {
    if (selector !== "button, input") throw new Error("unexpected selector " + selector);
    return fixedControls.concat(elements.get("models").children);
  },
};
const responseQueue = RESPONSES;
const fetchCalls = [];
function fetch(url, options = {}) {
  fetchCalls.push({url, options});
  if (!responseQueue.length) throw new Error("unexpected fetch: " + url);
  const item = responseQueue.shift();
  return Promise.resolve({
    ok: item.ok,
    status: item.status,
    json() {
      return item.jsonReject
        ? Promise.reject(new Error("invalid json"))
        : Promise.resolve(item.data);
    },
  });
}
function setInterval() { return 0; }
async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
  await new Promise((resolve) => setImmediate(resolve));
}
'''.replace("RESPONSES", response_json)
        program = harness + script + "\n" + expression
        completed = subprocess.run(
            [node, "-"],
            input=program,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)

    def test_actual_script_renders_fresh_loading_ready_and_hostile_model_as_text(self):
        output = self._execute(r'''
(async () => {
  await flush();
  const initial = {
    lifecycle: byId("lifecycle").textContent,
    active: byId("active-model").textContent,
    pending: byId("pending-model").textContent,
    inflight: byId("inflight").textContent,
    modelTexts: byId("models").children.map((child) => child.textContent),
  };
  render({
    models: ["a", "b"], active_model: null, pending_model: "b",
    lifecycle: "loading", inflight_requests: 3,
    settings: {automatic_switch: false, quick_switch: true, idle_timeout_seconds: 4.5},
  });
  const loading = {
    lifecycle: byId("lifecycle").textContent,
    pending: byId("pending-model").textContent,
    inflight: byId("inflight").textContent,
    automatic: byId("automatic-switch").checked,
    quick: byId("quick-switch").checked,
    timeout: byId("idle-timeout").value,
  };
  render({
    models: ["b"], active_model: "b", pending_model: null,
    lifecycle: "ready", inflight_requests: 0,
    settings: {automatic_switch: true, quick_switch: false, idle_timeout_seconds: 0},
  });
  const ready = {
    lifecycle: byId("lifecycle").textContent,
    active: byId("active-model").textContent,
    pending: byId("pending-model").textContent,
  };
  console.log(JSON.stringify({initial, loading, ready, remaining: responseQueue.length}));
})();
''')
        assert output == {
            "initial": {
                "lifecycle": "idle",
                "active": "–",
                "pending": "–",
                "inflight": "0",
                "modelTexts": ["a", "<img src=x onerror=alert(1)>"],
            },
            "loading": {
                "lifecycle": "loading",
                "pending": "b",
                "inflight": "3",
                "automatic": False,
                "quick": True,
                "timeout": "4.5",
            },
            "ready": {"lifecycle": "ready", "active": "b", "pending": "–"},
            "remaining": 0,
        }

    def test_actual_script_sends_exact_load_unload_and_complete_settings_requests(self):
        ready_a = {**FRESH, "active_model": "a", "lifecycle": "ready"}
        ready_b = {**FRESH, "active_model": "b", "lifecycle": "ready"}
        responses = [
            {"ok": True, "status": 200, "data": ready_a},
            {"ok": True, "status": 200, "data": ready_b},
            {"ok": True, "status": 200, "data": ready_b},
            {"ok": True, "status": 200, "data": FRESH},
            {"ok": True, "status": 200, "data": FRESH},
            {"ok": True, "status": 200, "data": {**FRESH, "settings": {"automatic_switch": False, "quick_switch": True, "idle_timeout_seconds": 42.5}}},
            {"ok": True, "status": 200, "data": FRESH},
        ]
        output = self._execute(r'''
(async () => {
  await flush();
  await loadModel("b");
  await unloadModel();
  byId("automatic-switch").checked = false;
  byId("quick-switch").checked = true;
  byId("idle-timeout").value = "42.5";
  await saveSettings();
  console.log(JSON.stringify({
    calls: fetchCalls,
    message: byId("message").textContent,
    controlsEnabled: document.querySelectorAll("button, input").every((node) => !node.disabled),
    remaining: responseQueue.length,
  }));
})();
''', responses)

        calls = output["calls"]
        assert [call["url"] for call in calls] == [
            "/api/v1/control/state",
            "/api/v1/control/model",
            "/api/v1/control/state",
            "/api/v1/control/model",
            "/api/v1/control/state",
            "/api/v1/control/settings",
            "/api/v1/control/state",
        ]
        assert calls[0]["options"] == {"cache": "no-store"}
        assert calls[1]["options"] == {
            "method": "PUT",
            "headers": {
                "X-Model-Switcher-Control": "1",
                "Content-Type": "application/json",
            },
            "body": '{"model":"b"}',
        }
        assert calls[3]["options"] == {
            "method": "DELETE",
            "headers": {"X-Model-Switcher-Control": "1"},
        }
        assert calls[5]["options"] == {
            "method": "PUT",
            "headers": {
                "X-Model-Switcher-Control": "1",
                "Content-Type": "application/json",
            },
            "body": '{"automatic_switch":false,"quick_switch":true,"idle_timeout_seconds":42.5}',
        }
        assert output["message"] == "Updated"
        assert output["controlsEnabled"] is True
        assert output["remaining"] == 0

    def test_actual_script_displays_api_error_without_html_injection_and_refreshes(self):
        hostile = '<svg onload="globalThis.pwned=true">denied</svg>'
        responses = [
            {"ok": True, "status": 200, "data": FRESH},
            {
                "ok": False,
                "status": 409,
                "data": {"error": {"message": hostile}},
            },
            {"ok": True, "status": 200, "data": FRESH},
        ]
        output = self._execute(r'''
(async () => {
  await flush();
  await loadModel("a");
  console.log(JSON.stringify({
    message: byId("message").textContent,
    className: byId("message").className,
    pwned: globalThis.pwned === true,
    urls: fetchCalls.map((call) => call.url),
    remaining: responseQueue.length,
  }));
})();
''', responses)
        assert output == {
            "message": hostile,
            "className": "error",
            "pwned": False,
            "urls": [
                "/api/v1/control/state",
                "/api/v1/control/model",
                "/api/v1/control/state",
            ],
            "remaining": 0,
        }
