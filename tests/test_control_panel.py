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
from src.stats import STATS_HTML
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
            'id="reload"',
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
  "unload", "reload", "automatic-switch", "quick-switch", "idle-timeout",
  "save-settings", "message"
];
const elements = new Map(ids.map((id) => [id, new Element("div", id)]));
const fixedControls = [
  elements.get("unload"), elements.get("reload"), elements.get("automatic-switch"),
  elements.get("quick-switch"), elements.get("idle-timeout"),
  elements.get("save-settings")
];
const document = {
  getElementById(id) { return elements.get(id) || null; },
  createElement(tagName) { return new Element(tagName); },
  querySelectorAll(selector) {
    if (selector !== "button, input") throw new Error("unexpected selector " + selector);
    const nested = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (child.tagName === "button") nested.push(child);
        walk(child);
      }
    };
    walk(elements.get("models"));
    return fixedControls.concat(nested);
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

    def test_actual_script_reloads_config_and_shows_summary(self):
        """P-20261013-04: Reload button posts with header, no body."""
        reloaded = {
            **FRESH,
            "models": ["a", "new"],
            "active_model": "a",
            "lifecycle": "ready",
            "reload": {
                "added": ["new"], "removed": ["old"], "changed": ["a"],
                "running_changed": True,
            },
        }
        removed = {
            **FRESH,
            "models": ["b"],
            "active_model": "a",
            "lifecycle": "ready",
            "reload": {
                "added": [], "removed": ["a"], "changed": [],
                "running_changed": True,
            },
        }
        plain = {
            **FRESH,
            "reload": {
                "added": [], "removed": [], "changed": [],
                "running_changed": False,
            },
        }
        responses = [
            {"ok": True, "status": 200, "data": FRESH},
            {"ok": True, "status": 200, "data": reloaded},
            {"ok": True, "status": 200, "data": reloaded},
            {"ok": True, "status": 200, "data": removed},
            {"ok": True, "status": 200, "data": removed},
            {"ok": True, "status": 200, "data": plain},
            {"ok": True, "status": 200, "data": FRESH},
        ]
        output = self._execute(r'''
(async () => {
  await flush();
  const messages = [];
  await byId("reload").click();
  messages.push(byId("message").textContent);
  const models = byId("models").children.map((child) => child.textContent);
  await reloadConfig();
  messages.push(byId("message").textContent);
  await reloadConfig();
  messages.push(byId("message").textContent);
  console.log(JSON.stringify({
    calls: fetchCalls, messages, models,
    className: byId("message").className,
    remaining: responseQueue.length,
  }));
})();
''', responses)
        calls = output["calls"]
        assert [call["url"] for call in calls] == [
            "/api/v1/control/state",
            "/api/v1/control/reload",
            "/api/v1/control/state",
            "/api/v1/control/reload",
            "/api/v1/control/state",
            "/api/v1/control/reload",
            "/api/v1/control/state",
        ]
        assert calls[1]["options"] == {
            "method": "POST",
            "headers": {"X-Model-Switcher-Control": "1"},
        }
        assert output["models"] == ["aLoaded", "new"]
        assert output["messages"] == [
            "Config reloaded.\nAdded: new\nRemoved: old\nChanged: a\n"
            "The running model keeps its old settings until its next start.",
            "Config reloaded.\nAdded: none\nRemoved: a\nChanged: none\n"
            "The running model was removed from the config; it keeps "
            "serving until you switch or unload it.",
            "Config reloaded.\nAdded: none\nRemoved: none\nChanged: none",
        ]
        assert output["className"] == ""
        assert output["remaining"] == 0

    def test_actual_script_shows_reload_error_as_text(self):
        message = "Config not reloaded: <b>bad</b> yaml"
        responses = [
            {"ok": True, "status": 200, "data": FRESH},
            {
                "ok": False,
                "status": 400,
                "data": {"error": {"message": message, "code": "config_invalid"}},
            },
            {"ok": True, "status": 200, "data": FRESH},
        ]
        output = self._execute(r'''
(async () => {
  await flush();
  await reloadConfig();
  console.log(JSON.stringify({
    message: byId("message").textContent,
    className: byId("message").className,
    remaining: responseQueue.length,
  }));
})();
''', responses)
        assert output == {
            "message": message, "className": "error", "remaining": 0,
        }


# ---------------------------------------------------------------------------
# P-20260926-02: /control looks like /stats and links to it
# ---------------------------------------------------------------------------


def _style(html: str) -> str:
    return html.split("<style>", 1)[1].split("</style>", 1)[0]


def _palette(style: str) -> tuple[dict, dict]:
    """(light, dark) custom-property values from a page stylesheet."""
    light, dark = style.split("@media (prefers-color-scheme: dark)", 1)
    pattern = r"(--[a-z0-9-]+):\s*([^;]+);"
    return dict(re.findall(pattern, light)), dict(re.findall(pattern, dark))


class TestPanelMatchesStatsLook:
    def test_header_links_to_stats_and_stats_links_back(self):
        header = CONTROL_HTML.split('<header class="top">', 1)[1].split("</header>", 1)[0]
        assert "<h1>Model Switcher</h1>" in header
        assert '<p class="subtitle">Control panel · <a href="/stats">Statistics</a></p>' in header
        assert '<a href="/control">Control panel</a>' in STATS_HTML

    def test_palette_type_and_width_match_stats(self):
        control_light, control_dark = _palette(_style(CONTROL_HTML))
        stats_light, stats_dark = _palette(_style(STATS_HTML))
        # Every colour both pages use has the same light and dark value.
        shared = set(control_light) & set(stats_light)
        assert {
            "--plane", "--surface", "--ink", "--ink-2", "--muted", "--hairline",
            "--baseline", "--ring", "--accent", "--good", "--serious",
            "--critical", "--good-tint", "--critical-tint", "--shadow",
        } <= shared
        for name in shared:
            assert control_light[name] == stats_light[name], name
        for name in set(control_dark) & set(stats_dark):
            assert control_dark[name] == stats_dark[name], name
        assert {"--plane", "--surface", "--ink", "--accent"} <= set(control_dark)
        style = _style(CONTROL_HTML)
        assert "main { max-width: 80rem;" in style
        assert "light-dark(" not in style
        body_font = 'font-family: system-ui, -apple-system, "Segoe UI", Roboto'
        assert body_font in style and body_font in _style(STATS_HTML)

    def test_status_tiles_and_section_cards_replace_fieldsets(self):
        assert "<fieldset" not in CONTROL_HTML
        tiles = CONTROL_HTML.split('<section class="kpis"', 1)[1].split("</section>", 1)[0]
        assert tiles.count('<div class="card tile">') == 4
        for ident in ("lifecycle", "active-model", "pending-model", "inflight"):
            assert f'id="{ident}"' in tiles
        assert CONTROL_HTML.count('<section class="card section">') == 2
        assert "<h2>Models</h2>" in CONTROL_HTML
        assert "<h2>Runtime settings</h2>" in CONTROL_HTML
        assert '<button id="save-settings" class="primary" type="button">' in CONTROL_HTML


class TestPanelStatusMarkers:
    _execute = staticmethod(TestExecutablePanelJavaScript._execute)

    def test_loaded_model_button_has_text_loaded_pill(self):
        output = self._execute(r'''
(async () => {
  await flush();
  render({
    models: ["a", "b"], active_model: "b", pending_model: null,
    lifecycle: "ready", inflight_requests: 0, settings: {},
  });
  const dump = (node) => ({
    tag: node.tagName, className: node.className, text: node.textContent,
    children: node.children.map(dump),
  });
  console.log(JSON.stringify(byId("models").children.map(dump)));
})();
''')
        plain, loaded = output
        assert plain["className"] == "model"
        assert plain["text"] == "a"
        assert [c["className"] for c in plain["children"]] == [""]
        assert loaded["className"] == "model loaded"
        label, pill = loaded["children"]
        assert label["text"] == "b"
        # Colour is always paired with a word: dot + "Loaded".
        assert pill["className"] == "pill"
        assert [(c["className"], c["text"]) for c in pill["children"]] == [
            ("dot good", ""), ("", "Loaded"),
        ]

    @pytest.mark.parametrize(
        ("lifecycle", "dot", "sub"),
        [
            ("ready", "dot good", "Serving"),
            ("loading", "dot serious", "Loading a model"),
            ("unloading", "dot serious", "Unloading"),
            ("idle", "dot", "No model loaded"),
            ("shutting_down", "dot serious", "Shutting down"),
            ("cleanup_failed", "dot critical", "Cleanup failed"),
            ("mystery", "dot", "Unknown state"),
            (None, "dot", "–"),
        ],
    )
    def test_lifecycle_tile_pairs_status_dot_with_text(self, lifecycle, dot, sub):
        output = self._execute(r'''
elements.set("lifecycle-dot", new Element("span", "lifecycle-dot"));
elements.set("lifecycle-sub", new Element("span", "lifecycle-sub"));
(async () => {
  await flush();
  render({models: [], lifecycle: LIFECYCLE_VALUE, settings: {}});
  console.log(JSON.stringify({
    value: byId("lifecycle").textContent,
    dot: byId("lifecycle-dot").className,
    sub: byId("lifecycle-sub").textContent,
  }));
})();
'''.replace("LIFECYCLE_VALUE", json.dumps(lifecycle)))
        assert output == {
            "value": lifecycle if lifecycle is not None else "–",
            "dot": dot,
            "sub": sub,
        }


# ---------------------------------------------------------------------------
# P-20261013-01: one button per variant for variant models
# ---------------------------------------------------------------------------


VARIANT_STATE = {
    **FRESH,
    "models": ["a", "qwen"],
    "variants": {"qwen": {
        "variants": ["text", "<b>vision</b>"],
        "default": "text",
        "active": "<b>vision</b>",
    }},
    "active_model": "qwen@text",
    "lifecycle": "ready",
}


class TestPanelVariants:
    _execute = staticmethod(TestExecutablePanelJavaScript._execute)

    def test_variant_model_renders_one_button_per_variant_with_markers(self):
        output = self._execute(r'''
(async () => {
  await flush();
  const dump = (node) => ({
    tag: node.tagName, className: node.className, text: node.textContent,
    children: node.children.map(dump),
  });
  console.log(JSON.stringify(byId("models").children.map(dump)));
})();
''', [{"ok": True, "status": 200, "data": VARIANT_STATE}])
        plain, group = output
        assert (plain["tag"], plain["className"], plain["text"]) == (
            "button", "model", "a"
        )
        assert (group["tag"], group["className"]) == ("div", "variant-group")
        title, text_button, vision_button = group["children"]
        assert (title["className"], title["text"]) == ("group-name", "qwen")
        # Loaded variant: same green "Loaded" pill as plain models.
        assert text_button["className"] == "model loaded"
        label, pill = text_button["children"]
        assert label["text"] == "text"
        assert pill["className"] == "pill"
        assert [(c["className"], c["text"]) for c in pill["children"]] == [
            ("dot good", ""), ("", "Loaded"),
        ]
        # Active (chosen) but not loaded: "Active" pill; hostile name is text.
        assert vision_button["className"] == "model"
        label, pill = vision_button["children"]
        assert label["text"] == "<b>vision</b>"
        assert pill["className"] == "pill active"
        assert [(c["className"], c["text"]) for c in pill["children"]] == [
            ("dot", ""), ("", "Active"),
        ]

    def test_variant_button_sends_model_and_variant(self):
        responses = [
            {"ok": True, "status": 200, "data": VARIANT_STATE},
            {"ok": True, "status": 200, "data": VARIANT_STATE},
            {"ok": True, "status": 200, "data": VARIANT_STATE},
        ]
        output = self._execute(r'''
(async () => {
  await flush();
  const group = byId("models").children[1];
  const states = [];
  group.children[2].click();
  states.push(document.querySelectorAll("button, input").every((n) => n.disabled));
  for (let i = 0; i < 5; i += 1) await flush();
  console.log(JSON.stringify({
    calls: fetchCalls,
    disabledWhileMutating: states[0],
    enabledAfter: document.querySelectorAll("button, input").every((n) => !n.disabled),
    remaining: responseQueue.length,
  }));
})();
''', responses)
        calls = output["calls"]
        assert [call["url"] for call in calls] == [
            "/api/v1/control/state",
            "/api/v1/control/model",
            "/api/v1/control/state",
        ]
        assert calls[1]["options"] == {
            "method": "PUT",
            "headers": {
                "X-Model-Switcher-Control": "1",
                "Content-Type": "application/json",
            },
            "body": '{"model":"qwen","variant":"<b>vision</b>"}',
        }
        assert output["disabledWhileMutating"] is True
        assert output["enabledAfter"] is True
        assert output["remaining"] == 0

    def test_state_without_variants_key_still_renders_plain_buttons(self):
        output = self._execute(r'''
(async () => {
  await flush();
  render({models: ["a", "b"], active_model: "b", lifecycle: "ready", settings: {}});
  console.log(JSON.stringify(byId("models").children.map((n) => n.textContent)));
})();
''')
        assert output == ["a", "bLoaded"]
