from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from skyvern.browser_extension.errors import BrowserExtensionError
from skyvern.browser_extension.package_extension import (
    EXTENSION_DIR,
    compute_extension_source_hash,
    package_extension,
    write_build_hash,
)
from skyvern.browser_extension.protocol import (
    ALLOWED_CDP_METHOD_PREFIXES,
    ALLOWED_EVENTS,
    ALLOWED_OPS,
    DENIED_CDP_METHODS,
    ERROR_CODES,
    EXTENSION_ID,
    PROTOCOL_VERSION,
    build_request,
    is_cdp_method_allowed,
    is_restricted_url,
    parse_extension_message,
)


def test_parse_valid_response() -> None:
    parsed = parse_extension_message(
        json.dumps({"v": 2, "type": "response", "id": "r-1", "ok": True, "result": {"tabId": 12}})
    )

    assert parsed.kind == "response"
    assert parsed.request_id == "r-1"
    assert parsed.ok is True
    assert parsed.result == {"tabId": 12}
    assert parsed.error_code is None


def test_parse_valid_event() -> None:
    parsed = parse_extension_message(
        json.dumps(
            {
                "v": 2,
                "type": "event",
                "event": "scope.tabAdded",
                "params": {"tabId": 12, "url": "https://example.com", "title": "Example"},
            }
        )
    )

    assert parsed.kind == "event"
    assert parsed.event == "scope.tabAdded"
    assert parsed.params == {"tabId": 12, "url": "https://example.com", "title": "Example"}


def test_parse_valid_ping() -> None:
    parsed = parse_extension_message('{"v":1,"type":"ping"}')

    assert parsed.kind == "ping"


def test_unknown_message_type_raises() -> None:
    with pytest.raises(BrowserExtensionError):
        parse_extension_message('{"v":1,"type":"auth.ok"}')


def test_protocol_allowlists_match_contract() -> None:
    assert PROTOCOL_VERSION == 2
    assert EXTENSION_ID == "dhommdmblflboaledbbfkdaapkadphlp"
    assert ALLOWED_OPS == frozenset(
        {
            "debugger.attach",
            "debugger.detach",
            "debugger.send",
            "dom.evaluate",
            "tabs.create",
            "tabs.remove",
            "tabs.activate",
            "tabs.list",
        }
    )
    assert ALLOWED_EVENTS == frozenset(
        {
            "extension.hello",
            "pairing.approved",
            "debugger.event",
            "debugger.detached",
            "scope.tabAdded",
            "scope.tabRemoved",
            "tabs.created",
        }
    )
    assert ERROR_CODES == frozenset(
        {
            "AUTH_FAILED",
            "OP_NOT_ALLOWED",
            "TAB_NOT_FOUND",
            "TAB_NOT_SCOPED",
            "RESTRICTED_URL",
            "ATTACH_FAILED",
            "DEBUGGER_DETACHED",
            "CDP_METHOD_NOT_ALLOWED",
            "CDP_ERROR",
            "COMMAND_TIMEOUT",
            "INTERNAL",
        }
    )


def test_manifest_key_derives_extension_id() -> None:
    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    manifest = json.loads((extension_dir / "manifest.json").read_text())
    public_key = base64.b64decode(manifest["key"], validate=True)
    digest_prefix = hashlib.sha256(public_key).hexdigest()[:32]
    derived_extension_id = "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest_prefix)

    assert derived_extension_id == EXTENSION_ID
    assert "userScripts" in manifest["permissions"]
    assert "activeTab" not in manifest["permissions"]
    assert "scripting" not in manifest["permissions"]
    assert manifest["minimum_chrome_version"] == "138"
    assert manifest["host_permissions"] == ["http://*/*", "https://*/*"]


def test_package_extension_builds_store_upload_zip(tmp_path: Path) -> None:
    output_path = package_extension(tmp_path / "skyvern-agent.zip")

    with zipfile.ZipFile(output_path) as archive:
        names = set(archive.namelist())
        packaged_manifest = json.loads(archive.read("manifest.json"))

    assert "key" not in packaged_manifest
    assert "service_worker.js" in names
    assert "dom_router.js" in names
    assert "README.md" not in names
    assert "build_hash.json" in names

    second_path = package_extension(tmp_path / "skyvern-agent-second.zip")
    assert output_path.read_bytes() == second_path.read_bytes()


def test_committed_build_hash_matches_current_sources() -> None:
    """Regenerate with `python -m skyvern.browser_extension.package_extension --write-build-hash`
    and commit the result whenever extension/** changes, or the broker will report every
    freshly loaded extension as stale."""
    committed = json.loads((EXTENSION_DIR / "build_hash.json").read_text())
    assert committed["sha256"] == compute_extension_source_hash(EXTENSION_DIR)


def test_compute_extension_source_hash_reacts_to_content_and_ignores_itself(tmp_path: Path) -> None:
    extension_dir = tmp_path / "extension"
    shutil.copytree(EXTENSION_DIR, extension_dir)

    baseline = compute_extension_source_hash(extension_dir)
    assert compute_extension_source_hash(extension_dir) == baseline

    (extension_dir / "build_hash.json").write_text(json.dumps({"sha256": "deliberately-wrong"}))
    assert compute_extension_source_hash(extension_dir) == baseline

    service_worker = extension_dir / "service_worker.js"
    service_worker.write_text(service_worker.read_text() + "\n// changed\n")
    assert compute_extension_source_hash(extension_dir) != baseline

    assert write_build_hash(extension_dir) == compute_extension_source_hash(extension_dir)
    assert json.loads((extension_dir / "build_hash.json").read_text())["sha256"] == compute_extension_source_hash(
        extension_dir
    )


def test_build_request_checks_operation_allowlist() -> None:
    assert build_request("r-3", "tabs.list", {}) == {
        "v": 2,
        "type": "request",
        "id": "r-3",
        "op": "tabs.list",
        "args": {},
    }

    with pytest.raises(BrowserExtensionError):
        build_request("r-4", "cookies.read", {})
    with pytest.raises(BrowserExtensionError):
        build_request(4, "tabs.list", {})  # type: ignore[arg-type]


def test_reset_ack_and_versioned_hello_contract() -> None:
    reset_ack = parse_extension_message(
        '{"v":2,"type":"extension.reset_ack","epoch":"daemon-epoch","generation":7,"ok":true}'
    )
    failed_reset_ack = parse_extension_message(
        '{"v":2,"type":"extension.reset_ack","epoch":"daemon-epoch","generation":8,"ok":false,"failedTabCount":2}'
    )
    hello = parse_extension_message(
        '{"v":2,"type":"event","event":"extension.hello",'
        '"params":{"protocolVersion":2,"extensionVersion":"1.0.0","scopedTabs":[]}}'
    )
    legacy_hello = parse_extension_message(
        '{"v":1,"type":"event","event":"extension.hello","params":{"extensionVersion":"0.9.0","scopedTabs":[]}}'
    )

    assert reset_ack.kind == "extension.reset_ack"
    assert reset_ack.reset_epoch == "daemon-epoch"
    assert reset_ack.generation == 7
    assert reset_ack.ok is True
    assert reset_ack.failed_tab_count == 0
    assert failed_reset_ack.reset_epoch == "daemon-epoch"
    assert failed_reset_ack.generation == 8
    assert failed_reset_ack.ok is False
    assert failed_reset_ack.failed_tab_count == 2
    assert hello.protocol_version == 2
    assert hello.params is not None and hello.params["protocolVersion"] == 2
    assert legacy_hello.protocol_version == 1
    assert legacy_hello.params is not None and "protocolVersion" not in legacy_hello.params

    with pytest.raises(BrowserExtensionError, match="protocolVersion"):
        parse_extension_message(
            '{"v":2,"type":"event","event":"extension.hello","params":{"extensionVersion":"1.0.0","scopedTabs":[]}}'
        )
    with pytest.raises(BrowserExtensionError, match="generation"):
        parse_extension_message('{"v":2,"type":"extension.reset_ack","epoch":"daemon-epoch","generation":-1,"ok":true}')
    with pytest.raises(BrowserExtensionError, match="epoch"):
        parse_extension_message('{"v":2,"type":"extension.reset_ack","epoch":"","generation":1,"ok":true}')
    with pytest.raises(BrowserExtensionError, match="failedTabCount"):
        parse_extension_message(
            '{"v":2,"type":"extension.reset_ack","epoch":"daemon-epoch","generation":1,"ok":false,"failedTabCount":0}'
        )


def test_extension_reset_ack_supports_idempotent_replay_and_verified_detach() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension reset contract test")
    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    bridge_uri = (extension_dir / "bridge_connection.js").as_uri()
    debugger_uri = (extension_dir / "debugger_router.js").as_uri()
    script = f"""
globalThis.chrome = {{
  alarms: {{ onAlarm: {{ addListener() {{}} }} }},
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
  }},
}};
globalThis.WebSocket = {{ OPEN: 1 }};
const {{ BridgeConnection }} = await import({json.dumps(bridge_uri)});
const {{ DebuggerRouter }} = await import({json.dumps(debugger_uri)});
const frames = [];
const resetResults = [
  {{ executed: false, ok: true, failedTabCount: 0 }},
  null,
  {{ executed: true, ok: false, failedTabCount: 1 }},
  {{ executed: true, ok: true, failedTabCount: 0 }},
];
const brokerEvents = [];
const bridge = new BridgeConnection({{
  onRequest: async () => ({{}}),
  onAuthenticated: async () => undefined,
  onReset: async () => resetResults.shift(),
  onEvent: async (event, params) => brokerEvents.push([event, params]),
  onStateChange: () => undefined,
}});
if (bridge.socket !== null || bridge.authenticated !== false) {{
  throw new Error("fresh bridge connection state was not initialized");
}}
bridge.authenticated = true;
bridge.socket = {{ readyState: 1, send: (raw) => frames.push(JSON.parse(raw)) }};
const resetFrame = (generation) => JSON.stringify({{
  v: 2,
  type: "extension.reset",
  epoch: "daemon-epoch",
  generation,
}});
await bridge.handleMessage(resetFrame(1));
if (frames[0]?.ok !== true || frames[0]?.generation !== 1) {{
  throw new Error("successfully executed reset identity was not re-acknowledged");
}}
await bridge.handleMessage(resetFrame(0));
if (frames.length !== 1) throw new Error("stale reset without a recorded outcome was acknowledged");
await bridge.handleMessage(resetFrame(2));
if (frames[1]?.ok !== false || frames[1]?.failedTabCount !== 1) {{
  throw new Error("failed reset acknowledgement was not fail-closed");
}}
await bridge.handleMessage(resetFrame(2));
if (frames[2]?.ok !== true || frames[2]?.epoch !== "daemon-epoch") {{
  throw new Error("failed reset identity did not re-execute successfully");
}}
await bridge.handleMessage(JSON.stringify({{
  v: 2,
  type: "event",
  event: "pairing.approved_ack",
  params: {{ approvalNonce: "approval-nonce", approved: true }},
}}));
if (
  brokerEvents[0]?.[0] !== "pairing.approved_ack" ||
  brokerEvents[0]?.[1]?.approvalNonce !== "approval-nonce"
) {{
  throw new Error("broker pairing approval acknowledgement was not dispatched");
}}

let targets = [{{ tabId: 7, attached: true }}];
chrome.debugger.detach = async () => {{ throw new Error("detach failed"); }};
chrome.debugger.getTargets = async () => targets;
const router = new DebuggerRouter({{
  tabScope: {{}},
  sendEvent: () => undefined,
  onAttachedChange: () => undefined,
}});
router.attachedTabs.add(7);
router.attachStates.set(7, {{ status: "attached" }});
const failed = await router.reset();
if (
  failed.failedTabCount !== 1 ||
  router.attachedTabs.has(7) ||
  router.attachStates.get(7)?.status !== "quarantined"
) {{
  throw new Error("live debugger attachment was not quarantined after detach failure");
}}
targets = [];
const benign = await router.reset();
if (benign.failedTabCount !== 0 || router.attachedTabs.has(7) || router.attachStates.has(7)) {{
  throw new Error("already-detached or missing target was not accepted");
}}
let detachCleanupCount = 0;
router.tabScope = {{
  runTabOperation: async (_tabId, operation) => operation(),
  assertScoped: async () => undefined,
  handleDebuggerDetachLocked: async () => {{ detachCleanupCount += 1; }},
}};
router.attachedTabs.add(8);
targets = [];
await router.detach({{ tabId: 8 }});
if (router.attachedTabs.has(8) || detachCleanupCount !== 1) {{
  throw new Error("already-detached cleanup did not release extension scope");
}}
await router.detach({{ tabId: 9 }});
if (detachCleanupCount !== 2) {{
  throw new Error("never-attached cleanup did not release extension scope");
}}
router.attachedTabs.add(10);
targets = [{{ tabId: 10, attached: true }}];
let detachError;
try {{
  await router.detach({{ tabId: 10 }});
}} catch (error) {{
  detachError = error;
}}
if (
  detachError?.code !== "CDP_ERROR" ||
  !router.attachedTabs.has(10) ||
  detachCleanupCount !== 2
) {{
  throw new Error("failed live detach did not remain fenced");
}}
"""

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_pairing_confirmation_renders_next_agent_offer_after_success() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the pairing confirmation contract test")
    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    pairing_uri = (extension_dir / "pairing_confirm.js").as_uri()
    script = f"""
class Element {{
  constructor() {{
    this.dataset = {{}};
    this.hidden = true;
    this.disabled = false;
    this.textContent = "";
    this.listeners = new Map();
    this.attributes = new Map();
  }}
  addEventListener(type, listener) {{ this.listeners.set(type, listener); }}
  setAttribute(name, value) {{ this.attributes.set(name, value); }}
  removeAttribute(name) {{ this.attributes.delete(name); }}
}}
const selectors = [
  "#pairing-question",
  "#pairing-port",
  "#pairing-fingerprint",
  "#approve-button",
  "#approve-label",
  "#cancel-button",
  ".actions",
  "#pairing-result",
  "#result-title",
  "#result-message",
  "#recovery",
];
const elements = Object.fromEntries(selectors.map((selector) => [selector, new Element()]));
globalThis.document = {{
  body: {{ dataset: {{}} }},
  querySelector: (selector) => elements[selector],
}};
let offer = {{
  port: 19777,
  token: "token",
  approvalNonce: "first-nonce",
  requestFingerprint: "first123",
}};
let storageListener = null;
globalThis.chrome = {{
  runtime: {{
    sendMessage: async () => ({{ ok: true, result: {{}} }}),
  }},
  storage: {{
    session: {{
      get: async () => ({{ pendingPairingOffer: offer }}),
    }},
    onChanged: {{
      addListener(listener) {{ storageListener = listener; }},
    }},
  }},
}};
await import({json.dumps(pairing_uri)});
if (elements["#pairing-fingerprint"].textContent !== "first123") {{
  throw new Error("first agent offer was not rendered");
}}
elements["#approve-button"].listeners.get("click")();
await new Promise((resolve) => setTimeout(resolve, 0));
if (elements["#result-title"].textContent !== "Connected") {{
  throw new Error("first agent approval did not reach its terminal state");
}}
offer = {{
  port: 19777,
  token: "token",
  approvalNonce: "second-nonce",
  requestFingerprint: "next4567",
}};
storageListener(
  {{ pendingPairingOffer: {{ newValue: offer }} }},
  "session",
);
await new Promise((resolve) => setTimeout(resolve, 0));
if (
  elements["#pairing-fingerprint"].textContent !== "next4567" ||
  elements["#approve-button"].disabled
) {{
  throw new Error("second agent offer did not replace the completed approval");
}}
"""

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_cdp_method_allowlist() -> None:
    assert all(prefix.endswith(".") for prefix in ALLOWED_CDP_METHOD_PREFIXES)
    assert is_cdp_method_allowed("Network.enable")
    assert is_cdp_method_allowed("Storage.getUsageAndQuota")
    assert is_cdp_method_allowed("Network.getCookies")
    assert not is_cdp_method_allowed("Network.getCookies", {"urls": []})
    assert not is_cdp_method_allowed("Browser.close")
    assert not is_cdp_method_allowed("SystemInfo.getInfo")
    assert not is_cdp_method_allowed("PageX.navigate")


@pytest.mark.parametrize(
    "method",
    [
        "Network.getAllCookies",
        "Network.clearBrowserCookies",
        "Network.clearBrowserCache",
        "Storage.getCookies",
        "Storage.setCookies",
        "Storage.clearCookies",
    ],
)
def test_cdp_method_denylist(method: str) -> None:
    assert method in DENIED_CDP_METHODS
    assert not is_cdp_method_allowed(method)


@pytest.mark.parametrize(
    ("url", "restricted"),
    [
        ("chrome://settings", True),
        ("about:blank", False),
        ("about:config", True),
        ("https://chromewebstore.google.com/detail/example", True),
        ("https://chromewebstore.google.com./detail/x", True),
        ("https://example.com", False),
        ("file:///tmp/example.html", True),
    ],
)
def test_restricted_url_matrix(url: str, restricted: bool) -> None:
    assert is_restricted_url(url) is restricted
