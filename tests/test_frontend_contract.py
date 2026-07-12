"""Lock frontend DOM contracts so UI refactors cannot drop bindings.

Rules:
- app.js uses $("id") => every referenced id must exist in templates/index.html
- tabs use data-tab + panel id with the same value
- required class hooks used by event delegation must remain
- frontend /api/* paths must exist as FastAPI routes (static path prefixes)

This test does not exercise browser behavior; it freezes the glue between
HTML, static/app.js, and backend routes before style/layout rewrites.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api import create_app
from app.store import StateStore

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "templates" / "index.html"
APP_JS = ROOT / "static" / "app.js"
HELPERS_JS = ROOT / "static" / "helpers.js"

# Visible tab panel ids. "import" was merged into test; activateTab remaps import→test.
REQUIRED_TABS = ("test", "assign", "dashboard")
LEGACY_IMPORT_ID = "import"

# Class hooks used by querySelector / event delegation in app.js
REQUIRED_CLASSES = (
    "tab",
    "panel",
    "node-check",
    "node-filter-tab",
    "node-target-check",
    "assign-check",
    "port-input",
)

# IDs created only inside JS-rendered HTML strings (not static template)
DYNAMIC_JS_IDS = frozenset({"checkAllNodes"})


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _html_ids(html: str) -> set[str]:
    return set(re.findall(r'\bid=["\']([^"\']+)["\']', html))


def _js_required_ids(js: str) -> set[str]:
    ids = set(re.findall(r'\$\(\s*["\']([A-Za-z0-9_-]+)["\']\s*\)', js))
    ids |= set(re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', js))
    ids |= set(re.findall(r'querySelector(?:All)?\(\s*["\']#([A-Za-z0-9_-]+)', js))
    return ids - DYNAMIC_JS_IDS


def _html_tabs(html: str) -> set[str]:
    return set(re.findall(r'data-tab=["\']([^"\']+)["\']', html))


def _frontend_api_paths(js: str) -> set[str]:
    """Collect static /api/... string literals (ignore template segments)."""
    paths = set()
    for raw in re.findall(r'["\'](/api/[^"\']+)["\']', js):
        # drop pure template leftovers if any
        if "${" in raw:
            continue
        paths.add(raw.split("?")[0])
    return paths


def _route_templates(app) -> set[str]:
    return {route.path for route in app.routes if isinstance(route, APIRoute)}


def _path_covered(frontend_path: str, route_templates: set[str]) -> bool:
    if frontend_path in route_templates:
        return True
    # /api/ports/8001/ip style dynamic segments
    for template in route_templates:
        if "{" not in template:
            continue
        pattern = re.sub(r"\{[^/]+\}", r"[^/]+", template)
        if re.fullmatch(pattern, frontend_path):
            return True
    # job cancel/poll paths built with template strings in JS; allow prefix match
    # against known job route roots when frontend only has static roots.
    return False


@pytest.fixture()
def client(tmp_path):
    store = StateStore(tmp_path / "assignments.json")
    app = create_app(store=store)
    return TestClient(app)


def test_index_contains_every_js_dom_id():
    html = _read(INDEX)
    js = _read(APP_JS) + "\n" + _read(HELPERS_JS)
    html_ids = _html_ids(html)
    required = _js_required_ids(js)
    missing = sorted(required - html_ids)
    assert not missing, (
        "static/app.js references DOM ids missing from templates/index.html:\n"
        + "\n".join(f"  - {item}" for item in missing)
    )


def test_tab_data_tab_matches_panel_ids():
    html = _read(INDEX)
    html_ids = _html_ids(html)
    tabs = _html_tabs(html)
    assert set(REQUIRED_TABS) <= tabs, f"missing data-tab values: {set(REQUIRED_TABS) - tabs}"
    assert "import" not in tabs, "import tab should be merged; only keep #import container id"
    missing_panels = [tab for tab in REQUIRED_TABS if tab not in html_ids]
    assert not missing_panels, f"tab panels missing id=: {missing_panels}"
    assert LEGACY_IMPORT_ID in html_ids, "keep #import for DOM id contract / nested import block"
    js = _read(APP_JS)
    assert 'tabId === "import"' in js or "tabId === 'import'" in js
    assert 'activateTab(localStorage.getItem(ACTIVE_TAB_KEY) || "test"' in js


def test_required_class_hooks_present_in_html():
    html = _read(INDEX)
    missing = [cls for cls in REQUIRED_CLASSES if not re.search(rf'\bclass=["\'][^"\']*\b{re.escape(cls)}\b', html)]
    # node-check / assign-check / port-input are rendered by app.js into #nodeTable etc.
    rendered_in_js = {"node-check", "assign-check", "port-input"}
    missing = [cls for cls in missing if cls not in rendered_in_js]
    for cls in rendered_in_js:
        assert cls in _read(APP_JS), f"JS no longer renders class '{cls}'"
    assert not missing, f"required class hooks missing from HTML: {missing}"


def test_frontend_api_paths_exist_on_backend(client):
    js = _read(APP_JS)
    frontend_paths = _frontend_api_paths(js)
    routes = _route_templates(client.app)
    # Also cover common dynamic job routes used via template strings
    dynamic_examples = {
        "/api/test/jobs/job-1",
        "/api/test/jobs/job-1/cancel",
        "/api/test-ports/jobs/job-1",
        "/api/test-ports/jobs/job-1/cancel",
        "/api/proxy-admin/jobs/job-1",
        "/api/proxy-admin/jobs/job-1/cancel",
        "/api/proxy-check/jobs/job-1",
        "/api/proxy-check/jobs/job-1/cancel",
        "/api/ports/8001/ip",
    }
    uncovered = sorted(
        path
        for path in sorted(frontend_paths | dynamic_examples)
        if not _path_covered(path, routes)
    )
    assert not uncovered, (
        "frontend calls API paths with no matching FastAPI route:\n"
        + "\n".join(f"  - {path}" for path in uncovered)
    )


def test_index_serves_required_contract_markers(client):
    response = client.get("/")
    assert response.status_code == 200
    text = response.text
    for tab in REQUIRED_TABS:
        assert f'data-tab="{tab}"' in text
        assert f'id="{tab}"' in text
    for element_id in (
        "urlInput",
        "importUrlBtn",
        "textInput",
        "importTextBtn",
        "import",
        "nodeTable",
        "assignTable",
        "portsTable",
        "engineToggleBtn",
        "engineState",
        "nodeCount",
        "mappingCount",
        "listeningCount",
        "notice",
    ):
        assert f'id="{element_id}"' in text
    assert 'data-tab="import"' not in text
    assert 'id="import"' in text
    assert 'class="import-block"' in text or "import-block" in text


def test_status_payload_has_fields_frontend_reads(client):
    """Freeze /api/status keys that static/app.js renderSummary depends on."""
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    required = {
        "running",
        "pid",
        "ready",
        "node_count",
        "mapping_count",
        "expected_ports",
        "listening_ports",
        "missing_ports",
        "config_ports",
        "config_matches_state",
        "proxy_connect_host",
        "web",
        "subscription",
    }
    missing = sorted(required - set(payload))
    assert not missing, f"/api/status missing keys used by frontend: {missing}"
    assert isinstance(payload["web"], dict)
    assert isinstance(payload["subscription"], dict)


def test_sing_box_update_controls_are_wired():
    html = _read(INDEX)
    js = _read(APP_JS) + "\n" + _read(HELPERS_JS)

    for element_id in ("singBoxVersionSummary", "checkSingBoxUpdateBtn", "updateSingBoxBtn", "rollbackSingBoxBtn"):
        assert f'id="{element_id}"' in html
        assert f'$("{element_id}")' in js
    assert "/api/engine/version" in js
    assert "/api/engine/update" in js
    assert "/api/engine/rollback" in js


def test_shadowsocks_protocol_uses_ss_chip_style():
    helpers = _read(HELPERS_JS)
    assert 'raw === "shadowsocks" ? "ss"' in helpers


def test_engine_start_stop_is_one_accessible_switch():
    html = _read(INDEX)
    js = _read(APP_JS)

    assert 'id="engineToggleBtn"' in html
    assert 'role="switch"' in html
    assert 'aria-checked="false"' in html
    assert 'engine-switch-label' not in html
    assert 'id="startBtn"' not in html
    assert 'id="stopBtn"' not in html
    assert 'running ? "/api/stop" : "/api/start"' in js


def test_dashboard_tool_cards_have_consistent_roles_and_icons():
    html = _read(INDEX)
    for cls in ("engine-tool", "doctor-tool", "ai-tool", "target-tool"):
        assert f'class="tool-section {cls}"' in html
    assert html.count('class="ico"') >= 4


def test_dashboard_results_have_one_visible_home():
    html = _read(INDEX)
    js = _read(APP_JS)

    assert 'id="localProxyCheckResult" class="tool-progress hidden"' in html
    assert 'showQuickResult("一键本地检测"' not in js
    assert 'showQuickResult("系统自检"' not in js
    assert 'localProxyPortSummary(port)' in js
    assert 'class="copy-command"' in js
    assert '>复制 curl</button>' in js
    assert 'class="node-identity"' in js
    assert 'class="mono server-address"' in js
    assert 'node-target-result' in js
