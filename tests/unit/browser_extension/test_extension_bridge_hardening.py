from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_WINDOW_GROUP_FAKES = """
function installWindowGroupFakes(chrome, tabs, updates) {
  const state = {
    types: new Map(), lookupFailures: new Set(), enumerationFailure: false,
    ungroupFails: false, ungroupNoop: false, groupCalls: [], alarms: [],
    attached: [], alarmListeners: [], focusedWindowId: 99,
    groupLookupFailures: new Set(), groupTitles: new Map(), pendingAlarms: new Map(), now: 0,
  };
  const allTypes = ["normal", "popup", "panel", "app", "devtools"];
  chrome.windows = {
    ...chrome.windows,
    async get(id, options) {
      assert.deepEqual(options.windowTypes, allTypes);
      if (state.lookupFailures.has(id)) throw new Error("window lookup failed");
      return { id, type: state.types.get(id) ?? "normal" };
    },
    async getAll(options) {
      assert.equal(options.populate, true);
      assert.deepEqual(options.windowTypes, allTypes);
      if (state.enumerationFailure) throw new Error("window enumeration failed");
      return [...new Set([...tabs.values()].map((tab) => tab.windowId))].map((id) => ({
        id, type: state.types.get(id) ?? "normal",
        tabs: [...tabs.values()].filter((tab) => tab.windowId === id).map((tab) => ({ ...tab })),
      }));
    },
  };
  chrome.tabs.onAttached = { addListener(fn) { state.attached.push(fn); } };
  const getGroup = chrome.tabGroups.get;
  chrome.tabGroups.get = async (id) => {
    if (state.groupLookupFailures.has(id)) throw new Error("group lookup failed");
    const group = await getGroup(id);
    return { ...group, title: state.groupTitles.get(id) ?? group.title };
  };
  chrome.alarms = {
    ...chrome.alarms,
    onAlarm: { addListener(fn) { state.alarmListeners.push(fn); } },
    async get(name) { return state.pendingAlarms.get(name); },
    async create(name, options) {
      state.alarms.push({ name, options });
      state.pendingAlarms.set(name, { name, scheduledTime: state.now + options.delayInMinutes * 60_000 });
    },
  };
  state.fireAlarm = (name) => {
    const alarm = state.pendingAlarms.get(name);
    assert(alarm, "no pending alarm");
    state.pendingAlarms.delete(name);
    state.alarmListeners.forEach((fn) => fn(alarm));
  };
  chrome.tabs.group = async (options) => {
    state.groupCalls.push(structuredClone(options));
    const windowId = options.createProperties?.windowId ??
      (options.groupId === undefined ? state.focusedWindowId : tabs.get(options.tabIds[0]).windowId);
    if ((state.types.get(windowId) ?? "normal") !== "normal") throw new Error("cannot group this window");
    for (const id of options.tabIds) Object.assign(tabs.get(id), { windowId, groupId: options.groupId ?? 700 });
    return options.groupId ?? 700;
  };
  chrome.tabs.ungroup = async (ids) => {
    if (state.ungroupFails) throw new Error("ungroup failed");
    if (!state.ungroupNoop) for (const id of ids) {
      tabs.get(id).groupId = -1;
      updates.forEach((fn) => fn(id, { groupId: -1 }));
    }
  };
  return state;
}
"""


@pytest.mark.parametrize("revocation", ["target_closed", "canceled_by_user", "unshared", "restricted_url"])
def test_created_tab_survives_scope_revocation_and_reset(revocation: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension scope lifecycle test")

    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    script = f"""
import assert from "node:assert/strict";
const tabs = new Map();
const stored = {{}};
const events = [];
const removed = [];
let nextId = 1;
const updates = [];
const listener = {{ addListener() {{}} }};
globalThis.chrome = {{
  tabs: {{
    onCreated: listener, onRemoved: listener,
    onUpdated: {{ addListener(fn) {{ updates.push(fn); }} }},
    async create({{ url }}) {{
      const tab = {{ id: nextId++, windowId: 1, groupId: -1, url, status: "complete" }};
      tabs.set(tab.id, tab);
      updates.forEach((fn) => fn(tab.id, {{ status: "complete" }}));
      return {{ ...tab }};
    }},
    async get(tabId) {{
      assert(tabs.has(tabId));
      return {{ ...tabs.get(tabId) }};
    }},
    async remove(tabId) {{ removed.push(tabId); tabs.delete(tabId); }},
  }},
  tabGroups: {{
    async query() {{ return []; }},
    async get(id) {{ return {{ id, title: "Skyvern Controlled" }}; }},
    async update() {{}},
  }},
  storage: {{ session: {{
    async get(defaults) {{ return {{ ...defaults, ...stored }}; }},
    async set(values) {{ Object.assign(stored, structuredClone(values)); }},
    async remove(keys) {{ for (const key of keys) delete stored[key]; }},
  }} }},
  debugger: {{ onEvent: listener, onDetach: listener }},
}};
{_WINDOW_GROUP_FAKES}
installWindowGroupFakes(chrome, tabs, updates);
const {{ TabScope }} = await import({json.dumps((extension_dir / "tab_scope.js").as_uri())});
const {{ DebuggerRouter }} = await import({json.dumps((extension_dir / "debugger_router.js").as_uri())});
const scope = new TabScope({{ sendEvent: (event, params) => events.push({{ event, params }}) }});
await scope.initialize();
const router = new DebuggerRouter({{
  tabScope: scope, sendEvent: () => undefined, onAttachedChange: () => undefined,
}});
scope.setDebuggerRouter(router);
const {{ tabId }} = await scope.create({{ url: "https://handoff.example.test" }});
assert(stored.createdTabIds.includes(tabId));
const revocation = {json.dumps(revocation)};
if (revocation === "unshared") {{
  await scope.unshareTab(tabId);
}} else if (revocation === "restricted_url") {{
  tabs.get(tabId).url = "chrome://settings";
  await scope.handleTabUpdated(tabId, {{ url: "chrome://settings" }});
}} else {{
  router.attachedTabs.add(tabId);
  router.attachStates.set(tabId, {{ status: "attached" }});
  await router.handleDebuggerDetach({{ tabId }}, revocation);
}}
assert(tabs.has(tabId), "revocation must leave the physical tab open");
assert(!scope.isScoped(tabId));
assert(!router.attachedTabs.has(tabId));
assert.equal(tabs.get(tabId).groupId, -1);
assert(!stored.createdTabIds.includes(tabId), "reset must not retain ownership of a handed-back tab");
await assert.rejects(scope.assertScoped(tabId), {{ code: "TAB_NOT_SCOPED" }});
await assert.rejects(scope.remove({{ tabId }}), {{ code: "TAB_NOT_SCOPED" }});
assert(events.some((entry) => entry.event === "scope.tabRemoved" && entry.params.tabId === tabId));

// MV3 restoration and the next broker reset must preserve the handed-back tab.
const restored = new TabScope({{ sendEvent: () => undefined }});
await restored.initialize();
await restored.prepareForReset();
assert.equal((await restored.reset()).failedTabCount, 0);
restored.finishReset();
assert(tabs.has(tabId));
assert.deepEqual(removed, []);

// The operator can explicitly share it again; it now has user-shared ownership.
tabs.get(tabId).url = "https://handoff.example.test";
await restored.shareTab(tabId);
assert(restored.isScoped(tabId));
assert(!stored.createdTabIds.includes(tabId));
await restored.prepareForReset();
await restored.reset();
restored.finishReset();
assert(tabs.has(tabId));

// Explicit removal of an actively scoped agent-created tab still works.
const created = await restored.create({{ url: "about:blank" }});
await restored.remove(created);
assert(!tabs.has(created.tabId));
assert.deepEqual(removed, [created.tabId]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_extension_request_isolation_timeouts_and_mv3_reconnect_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension bridge contract test")

    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    bridge_uri = (extension_dir / "bridge_connection.js").as_uri()
    debugger_uri = (extension_dir / "debugger_router.js").as_uri()
    protocol_uri = (extension_dir / "protocol.js").as_uri()
    script = f"""
const alarmListeners = [];
const alarmCreates = [];
globalThis.chrome = {{
  alarms: {{
    onAlarm: {{ addListener(listener) {{ alarmListeners.push(listener); }} }},
    async create(name, options) {{ alarmCreates.push({{ name, options }}); }},
  }},
  storage: {{
    local: {{
      async get(defaults) {{ return {{ ...defaults, enabled: false, pairingToken: "token" }}; }},
      async set() {{}},
    }},
  }},
  debugger: {{
    onEvent: {{ addListener() {{}} }},
    onDetach: {{ addListener() {{}} }},
    async getTargets() {{ return []; }},
  }},
}};
globalThis.WebSocket = {{ OPEN: 1, CONNECTING: 0, CLOSING: 2 }};

const {{ BridgeConnection, nextReconnectDelay }} = await import({json.dumps(bridge_uri)});
const {{ DebuggerRouter }} = await import({json.dumps(debugger_uri)});
const {{ BRIDGE_ALARM_NAME, ERROR_CODES }} = await import({json.dumps(protocol_uri)});

const waitUntil = async (predicate, timeoutMs = 250) => {{
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {{
    if (Date.now() >= deadline) throw new Error("condition timed out");
    await new Promise((resolve) => setTimeout(resolve, 1));
  }}
}};

// A page command that never settles must not hold the global inbound-message chain.
let releaseWedged;
const frames = [];
const bridge = new BridgeConnection({{
  onRequest: async (op) => {{
    if (op === "debugger.send") {{
      return new Promise((resolve) => {{ releaseWedged = resolve; }});
    }}
    return {{ tabs: [] }};
  }},
  onAuthenticated: async () => undefined,
  onReset: async () => ({{ executed: true, ok: true, failedTabCount: 0 }}),
  onStateChange: () => undefined,
  requestTimeoutMs: 5,
}});
bridge.authenticated = true;
const socket = {{ readyState: 1, send: (raw) => frames.push(JSON.parse(raw)) }};
bridge.socket = socket;
bridge.connectionGeneration = 7;
bridge.enqueueIncomingMessage(
  JSON.stringify({{ v: 2, type: "request", id: "stuck", op: "debugger.send", args: {{}} }}),
  7,
  socket,
);
bridge.enqueueIncomingMessage(
  JSON.stringify({{ v: 2, type: "request", id: "list", op: "tabs.list", args: {{}} }}),
  7,
  socket,
);
await waitUntil(() => frames.some((frame) => frame.id === "list"));
if (frames.find((frame) => frame.id === "list")?.ok !== true) {{
  throw new Error("independent request did not complete successfully");
}}
if (frames.some((frame) => frame.id === "stuck")) {{
  throw new Error("wedged request unexpectedly completed");
}}
await waitUntil(() => frames.some((frame) => frame.id === "stuck"));
const timeoutFrame = frames.find((frame) => frame.id === "stuck");
if (timeoutFrame?.ok !== false || timeoutFrame?.error?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`request timeout was not structured: ${{JSON.stringify(timeoutFrame)}}`);
}}
releaseWedged({{}});
await new Promise((resolve) => setTimeout(resolve, 5));
if (frames.filter((frame) => frame.id === "stuck").length !== 1) {{
  throw new Error("settling a timed-out operation sent a duplicate response");
}}

// MV3 eviction recovery must have a persistent 30-second alarm plus bounded backoff.
await bridge.initialize();
const alarm = alarmCreates.find((entry) => entry.name === BRIDGE_ALARM_NAME);
if (alarm?.options?.periodInMinutes !== 0.5) {{
  throw new Error(`unexpected reconnect alarm period: ${{alarm?.options?.periodInMinutes}}`);
}}
const backoff = [1_000];
for (let index = 0; index < 7; index += 1) backoff.push(nextReconnectDelay(backoff.at(-1)));
if (JSON.stringify(backoff) !== JSON.stringify([1_000, 2_000, 4_000, 8_000, 16_000, 30_000, 30_000, 30_000])) {{
  throw new Error(`unexpected reconnect backoff: ${{JSON.stringify(backoff)}}`);
}}
let alarmKicks = 0;
bridge.kick = async () => {{ alarmKicks += 1; }};
alarmListeners.forEach((listener) => listener({{ name: BRIDGE_ALARM_NAME }}));
await waitUntil(() => alarmKicks === 1);

const allowedUrlChanges = [];
let revokedUrlChangeCount = 0;
const tabScope = {{
  async runTabOperation(_tabId, operation) {{
    return operation({{
      isCurrent: () => true,
      assertCurrent() {{}},
      allowUrlChange() {{ allowedUrlChanges.push("granted"); }},
      revokeUrlChange() {{ revokedUrlChangeCount += 1; }},
    }});
  }},
  async assertControllableLocked() {{}},
  async assertScoped() {{}},
  isScoped() {{ return true; }},
  async handleDebuggerDetachLocked() {{}},
}};
const events = [];
const router = new DebuggerRouter({{
  tabScope,
  sendEvent: (event, params) => events.push({{ event, params }}),
  onAttachedChange: () => undefined,
  attachTimeoutMs: 5,
  commandTimeoutMs: 5,
  recoveryTimeoutMs: 5,
}});

// Attach has a bounded, explicit failure state. A different adopted tab can still attach.
chrome.debugger.attach = ({{ tabId }}) =>
  tabId === 11
    ? new Promise(() => undefined)
    : tabId === 13
      ? Promise.reject(new Error("another debugger owns this tab"))
      : Promise.resolve();
let attachError;
try {{
  await router.attach({{ tabId: 11 }});
}} catch (error) {{
  attachError = error;
}}
if (attachError?.code !== ERROR_CODES.ATTACH_FAILED || !attachError.message.includes("timed out")) {{
  throw new Error(`attach failure was not structured: ${{attachError?.code}} ${{attachError?.message}}`);
}}
if (router.attachStates.get(11)?.status !== "orphaned_attach") {{
  throw new Error("attach state machine did not quarantine the unresolved attempt");
}}
let rejectedAttachError;
try {{
  await router.attach({{ tabId: 13 }});
}} catch (error) {{
  rejectedAttachError = error;
}}
if (
  rejectedAttachError?.code !== ERROR_CODES.ATTACH_FAILED ||
  !rejectedAttachError.message.includes("another debugger owns this tab")
) {{
  throw new Error(`rejected attach was not structured: ${{rejectedAttachError?.code}}`);
}}
await router.attach({{ tabId: 12 }});
if (!router.attachedTabs.has(12) || router.attachStates.get(12)?.status !== "attached") {{
  throw new Error("a healthy adopted tab did not complete the normal attach flow");
}}

// A wedged CDP command returns COMMAND_TIMEOUT, attempts detach recovery, and does not block another tab.
router.attachedTabs.add(21);
router.attachStates.set(21, {{ status: "attached" }});
router.attachedTabs.add(22);
router.attachStates.set(22, {{ status: "attached" }});
const detached = [];
chrome.debugger.detach = async ({{ tabId }}) => {{ detached.push(tabId); }};
chrome.debugger.sendCommand = ({{ tabId }}, method) =>
  tabId === 21 ? new Promise(() => undefined) : Promise.resolve({{ method }});
let commandError;
try {{
  await router.send({{ tabId: 21, method: "Runtime.evaluate", params: {{ expression: "while(true){{}}" }} }});
}} catch (error) {{
  commandError = error;
}}
if (commandError?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`command timeout was not structured: ${{commandError?.code}}`);
}}
if (!detached.includes(21) || router.attachedTabs.has(21)) {{
  throw new Error("timed-out command did not execute bounded detach recovery");
}}

// A timed-out child-session command must preserve the tab and child tracking.
router.attachedTabs.add(23);
router.attachStates.set(23, {{ status: "attached" }});
const childSessionId = "child-session-23";
router.childTargets.set(childSessionId, {{ tabId: 23, type: "iframe" }});
chrome.debugger.sendCommand = (target, method) =>
  target.sessionId === childSessionId
    ? new Promise(() => undefined)
    : Promise.resolve({{ method }});
let childCommandError;
try {{
  await router.send({{
    tabId: 23,
    sessionId: childSessionId,
    method: "Page.createIsolatedWorld",
    params: {{ frameId: 123, worldName: "test" }},
  }});
}} catch (error) {{
  childCommandError = error;
}}
if (childCommandError?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`child command timeout was not structured: ${{childCommandError?.code}}`);
}}
if (
  detached.includes(23) ||
  !router.attachedTabs.has(23) ||
  router.attachStates.get(23)?.status !== "attached" ||
  !router.childTargets.has(childSessionId)
) {{
  throw new Error("timed-out child command changed tab or child-session state");
}}
const rootAfterChildTimeout = await router.send({{
  tabId: 23,
  method: "Runtime.evaluate",
  params: {{ expression: "2+2" }},
}});
if (rootAfterChildTimeout.result.method !== "Runtime.evaluate") {{
  throw new Error("root command did not complete after child timeout");
}}
const healthy = await router.send({{ tabId: 22, method: "Runtime.evaluate", params: {{ expression: "2+2" }} }});
if (healthy.result.method !== "Runtime.evaluate") {{
  throw new Error("healthy tab command was blocked by a wedged tab");
}}
for (const [method, params] of [
  ["Page.navigate", {{ url: "https://example.test/next" }}],
  ["Page.navigateToHistoryEntry", {{ entryId: 1 }}],
  ["Page.reload", {{}}],
]) {{
  const navigation = await router.send({{ tabId: 22, method, params }});
  if (navigation.result.method !== method) {{
    throw new Error(`${{method}} did not complete`);
  }}
}}
if (JSON.stringify(allowedUrlChanges) !== JSON.stringify(["granted", "granted", "granted"])) {{
  throw new Error(`URL-changing CDP methods granted the wrong URL bindings: ${{JSON.stringify(allowedUrlChanges)}}`);
}}
if (revokedUrlChangeCount !== 0) {{
  throw new Error("successful navigations must not revoke their URL-change grants");
}}
chrome.debugger.sendCommand = () => Promise.reject(new Error("navigation rejected"));
let failedNavigationError;
try {{
  await router.send({{ tabId: 22, method: "Page.navigate", params: {{ url: "https://example.test/failed" }} }});
}} catch (error) {{
  failedNavigationError = error;
}}
if (failedNavigationError?.code !== ERROR_CODES.CDP_ERROR) {{
  throw new Error(`failed navigation was not structured: ${{failedNavigationError?.code}}`);
}}
if (revokedUrlChangeCount !== 1) {{
  throw new Error("failed navigation did not revoke its URL-change grant");
}}
"""

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_dom_evaluate_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension DOM evaluation contract test")

    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    dom_uri = (extension_dir / "dom_router.js").as_uri()
    protocol_uri = (extension_dir / "protocol.js").as_uri()
    script = f"""
const scriptCalls = [];
globalThis.chrome = {{
  userScripts: {{
    async execute({{ target, world, injectImmediately, js }}) {{
      scriptCalls.push({{ target, world, injectImmediately, js }});
      try {{
        return [{{ result: await globalThis.eval(js[0].code) }}];
      }} catch (error) {{
        return [{{ error: String(error?.message ?? error) }}];
      }}
    }},
  }},
}};

const {{ evaluateDom }} = await import({json.dumps(dom_uri)});
const {{ ERROR_CODES, ProtocolError }} = await import({json.dumps(protocol_uri)});

let leaseChecks = 0;
let controllableChecks = 0;
let controllableUrl = "https://example.test/";
const lease = {{ assertCurrent() {{ leaseChecks += 1; }} }};
const tabScope = {{
  async runTabOperation(tabId, operation) {{
    if (tabId !== 7) throw new Error(`unexpected tab ID: ${{tabId}}`);
    return operation(lease);
  }},
  async assertControllableLocked(tabId, currentLease) {{
    if (tabId !== 7 || currentLease !== lease) throw new Error("scope check lost the tab lease");
    controllableChecks += 1;
    return {{ id: tabId, groupId: 700, url: controllableUrl }};
  }},
}};

const evaluated = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "({{ answer: 6 * 7 }})",
}});
if (
  evaluated.result?.answer !== 42 ||
  scriptCalls.length !== 1 ||
  scriptCalls[0].target?.tabId !== 7 ||
  scriptCalls[0].world !== "MAIN" ||
  scriptCalls[0].injectImmediately !== true ||
  scriptCalls[0].js?.[0]?.code !== "({{ answer: 6 * 7 }})" ||
  controllableChecks !== 2 ||
  leaseChecks !== 1
) {{
  throw new Error(`DOM evaluation contract failed: ${{JSON.stringify({{
    evaluated,
    scriptCalls,
    controllableChecks,
    leaseChecks,
  }})}}`);
}}

for (const [expression, expected] of [["false", false], ["0", 0], ["null", null]]) {{
  const response = await evaluateDom(tabScope, {{ tabId: 7, expression }});
  if (!Object.is(response.result, expected)) {{
    throw new Error(`DOM evaluation changed a falsy result: ${{expression}}`);
  }}
}}
const thrownEvaluationError = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "(() => {{ throw new Error('page failure'); }})()",
}}).then(() => null, (error) => error);
if (
  thrownEvaluationError?.code !== ERROR_CODES.CDP_ERROR ||
  !thrownEvaluationError.message.includes("page failure")
) {{
  throw new Error(`thrown page evaluation was not structured: ${{thrownEvaluationError?.message}}`);
}}

const invalidError = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "1 + 1",
  extra: true,
}}).then(() => null, (error) => error);
if (invalidError?.code !== ERROR_CODES.OP_NOT_ALLOWED) {{
  throw new Error(`invalid DOM arguments did not fail closed: ${{invalidError?.code}}`);
}}

const callsBeforeBlank = scriptCalls.length;
controllableUrl = "about:blank";
const blankError = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "1 + 1",
}}).then(() => null, (error) => error);
if (
  blankError?.code !== ERROR_CODES.RESTRICTED_URL ||
  scriptCalls.length !== callsBeforeBlank
) {{
  throw new Error(`about:blank reached MAIN evaluation: ${{blankError?.code}}`);
}}
controllableUrl = "https://example.test/";

chrome.userScripts.execute = async () => {{ throw new Error("injection blocked"); }};
const injectionError = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "1 + 1",
}}).then(() => null, (error) => error);
if (injectionError?.code !== ERROR_CODES.CDP_ERROR) {{
  throw new Error(`failed injection was not structured: ${{injectionError?.code}}`);
}}

let revalidationChecks = 0;
let failedInjectionCalls = 0;
chrome.userScripts.execute = async () => {{
  failedInjectionCalls += 1;
  throw new Error("navigation interrupted injection");
}};
tabScope.assertControllableLocked = async (tabId, currentLease) => {{
  if (tabId !== 7 || currentLease !== lease) throw new Error("scope check lost the tab lease");
  revalidationChecks += 1;
  if (revalidationChecks === 2) {{
    throw new ProtocolError(
      ERROR_CODES.RESTRICTED_URL,
      "Chrome does not allow controlling this URL.",
    );
  }}
  return {{ id: tabId, groupId: 700, url: "https://example.test/" }};
}};
const revokedError = await evaluateDom(tabScope, {{
  tabId: 7,
  expression: "1 + 1",
}}).then(() => null, (error) => error);
if (
  revokedError?.code !== ERROR_CODES.RESTRICTED_URL ||
  revalidationChecks !== 2 ||
  failedInjectionCalls !== 1
) {{
  throw new Error(`failed injection hid scope revocation: ${{JSON.stringify({{
    code: revokedError?.code,
    revalidationChecks,
    failedInjectionCalls,
  }})}}`);
}}
"""

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_extension_reset_and_debugger_lifecycle_contract() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension bridge lifecycle test")

    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    bridge_uri = (extension_dir / "bridge_connection.js").as_uri()
    debugger_uri = (extension_dir / "debugger_router.js").as_uri()
    protocol_uri = (extension_dir / "protocol.js").as_uri()
    tab_scope_uri = (extension_dir / "tab_scope.js").as_uri()
    script = f"""
import assert from "node:assert/strict";
const listeners = {{ created: [], removed: [], updated: [], debuggerEvent: [], debuggerDetach: [] }};
const tabs = new Map();
const sessionState = {{}};
const debuggerAttached = new Set();
let createTab;
let lastFocusedWindowId = 2;
globalThis.chrome = {{
  alarms: {{ onAlarm: {{ addListener() {{}} }} }},
  tabs: {{
    onCreated: {{ addListener(listener) {{ listeners.created.push(listener); }} }},
    onRemoved: {{ addListener(listener) {{ listeners.removed.push(listener); }} }},
    onUpdated: {{ addListener(listener) {{ listeners.updated.push(listener); }} }},
    create({{ url }}) {{
      if (sessionState.pendingTabCreation?.url !== url) throw new Error("creation marker was not durable before Chrome invocation");
      return new Promise((resolve) => {{ createTab = resolve; }});
    }},
    async get(tabId) {{
      const tab = tabs.get(tabId);
      if (!tab) throw new Error("missing tab");
      return {{ ...tab }};
    }},
    async query(values) {{
      return [...tabs.values()]
        .filter((tab) => !values.active || tab.active === true)
        .filter((tab) => !values.lastFocusedWindow || tab.windowId === lastFocusedWindowId)
        .map((tab) => ({{ ...tab }}));
    }},
    async remove(tabId) {{ tabs.delete(tabId); }},
    async update(tabId, values) {{ Object.assign(tabs.get(tabId), values); }},
  }},
  tabGroups: {{
    async query() {{ return []; }},
    async get(groupId) {{ return {{ id: groupId, title: "Skyvern Controlled" }}; }},
    async update() {{}},
  }},
  windows: {{ async update() {{}} }},
  storage: {{
    session: {{
      async get(defaults) {{ return {{ ...defaults, ...sessionState }}; }},
      async set(values) {{ Object.assign(sessionState, values); }},
      async remove(keys) {{ for (const key of keys) delete sessionState[key]; }},
    }},
  }},
  debugger: {{
    onEvent: {{ addListener(listener) {{ listeners.debuggerEvent.push(listener); }} }},
    onDetach: {{ addListener(listener) {{ listeners.debuggerDetach.push(listener); }} }},
    async getTargets() {{
      return [...debuggerAttached].map((tabId) => ({{ tabId, attached: true }}));
    }},
  }},
}};
globalThis.WebSocket = {{ OPEN: 1, CONNECTING: 0, CLOSING: 2 }};

{_WINDOW_GROUP_FAKES}
installWindowGroupFakes(chrome, tabs, listeners.updated);
const {{ BridgeConnection }} = await import({json.dumps(bridge_uri)});
const {{ DebuggerRouter }} = await import({json.dumps(debugger_uri)});
const {{ ERROR_CODES }} = await import({json.dumps(protocol_uri)});
const {{ TabScope }} = await import({json.dumps(tab_scope_uri)});

const delay = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
const waitUntil = async (predicate, timeoutMs = 250) => {{
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {{
    if (Date.now() >= deadline) throw new Error("condition timed out");
    await delay(1);
  }}
}};
const settleWithin = (promise, timeoutMs = 100) => Promise.race([
  promise,
  delay(timeoutMs).then(() => {{ throw new Error("promise did not settle"); }}),
]);

const scopeEvents = [];
const scope = new TabScope({{
  sendEvent: (event, params) => scopeEvents.push({{ event, params }}),
  operationTimeoutMs: 20,
}});
await scope.initialize();
// tabs.list marks only the active tab in Chrome's last-focused window.
tabs.set(18, {{ id: 18, windowId: 1, groupId: 700, url: "https://one.example", active: true }});
tabs.set(19, {{ id: 19, windowId: 2, groupId: 700, url: "https://two.example", active: true }});
scope.scopedTabIds.add(18);
scope.scopedTabIds.add(19);
scope.scopedGroupIds.set(18, 700);
scope.scopedGroupIds.set(19, 700);
const focusedTabs = (await scope.list()).tabs.filter((tab) => tab.active);
if (focusedTabs.length !== 1 || focusedTabs[0].tabId !== 19) {{
  throw new Error(`focused tab selection was ambiguous: ${{JSON.stringify(focusedTabs)}}`);
}}
scope.scopedTabIds.delete(18);
scope.scopedTabIds.delete(19);
scope.scopedGroupIds.delete(18);
scope.scopedGroupIds.delete(19);
tabs.delete(18);
tabs.delete(19);

// Popup admission must survive the initial URL event during its first persistence.
tabs.set(40, {{ id: 40, windowId: 1, groupId: 700, url: "https://popup-opener.example" }});
tabs.set(41, {{ id: 41, openerTabId: 40, windowId: 1, groupId: -1, url: "https://popup.example" }});
scope.scopedTabIds.add(40);
scope.scopedGroupIds.set(40, 700);
let popupInitialPersistStarted = false;
const originalSessionSet = chrome.storage.session.set;
chrome.storage.session.set = async (values) => {{
  if (!popupInitialPersistStarted && values.createdTabIds?.includes(41)) {{
    popupInitialPersistStarted = true;
    listeners.updated.forEach((listener) =>
      listener(41, {{ url: "https://popup.example" }}),
    );
  }}
  return originalSessionSet(values);
}};
const popupAdmission = scope.handleTabCreated(tabs.get(41)).then(
  () => null,
  (error) => error,
);
const popupAdmissionOutcome = await settleWithin(popupAdmission);
chrome.storage.session.set = originalSessionSet;
const popupCreatedEvents = scopeEvents.filter(
  (entry) => entry.event === "tabs.created" && entry.params?.tabId === 41,
);
if (
  !popupInitialPersistStarted ||
  popupAdmissionOutcome !== null ||
  popupCreatedEvents.length !== 1 ||
  !scope.scopedTabIds.has(41) ||
  !scope.createdTabIds.has(41) ||
  !tabs.has(41)
) {{
  throw new Error(`popup admission failed: ${{JSON.stringify({{
    persisted: popupInitialPersistStarted,
    outcome: popupAdmissionOutcome?.code ?? popupAdmissionOutcome,
    createdEvents: popupCreatedEvents,
    scoped: scope.scopedTabIds.has(41),
    owned: scope.createdTabIds.has(41),
    open: tabs.has(41),
  }})}}`);
}}
scope.scopedTabIds.delete(40);
scope.scopedGroupIds.delete(40);
scope.scopedTabIds.delete(41);
scope.scopedGroupIds.delete(41);
scope.createdTabIds.delete(41);
await scope.persistScope();
tabs.delete(40);
tabs.delete(41);

// A persistence failure during popup admission must close and unown the popup.
tabs.set(42, {{ id: 42, windowId: 1, groupId: 700, url: "https://popup-failure-opener.example" }});
tabs.set(43, {{ id: 43, openerTabId: 42, windowId: 1, groupId: -1, url: "https://popup-failure.example" }});
scope.scopedTabIds.add(42);
scope.scopedGroupIds.set(42, 700);
let forcedPopupPersistenceFailure = false;
chrome.storage.session.set = async (values) => {{
  if (!forcedPopupPersistenceFailure && values.createdTabIds?.includes(43)) {{
    forcedPopupPersistenceFailure = true;
    listeners.updated.forEach((listener) =>
      listener(43, {{ url: "https://popup-failure.example" }}),
    );
    throw new Error("forced popup persistence failure");
  }}
  return originalSessionSet(values);
}};
const failedPopupAdmission = scope.handleTabCreated(tabs.get(43)).then(
  () => null,
  (error) => error,
);
const failedPopupOutcome = await settleWithin(failedPopupAdmission);
chrome.storage.session.set = originalSessionSet;
await waitUntil(() => !scope.tabOperationLeases.has(43));
if (
  !forcedPopupPersistenceFailure ||
  failedPopupOutcome?.message !== "forced popup persistence failure" ||
  scope.scopedTabIds.has(43) ||
  scope.createdTabIds.has(43) ||
  tabs.has(43)
) {{
  throw new Error(`failed popup was not cleaned up: ${{JSON.stringify({{
    forced: forcedPopupPersistenceFailure,
    outcome: failedPopupOutcome?.message,
    scoped: scope.scopedTabIds.has(43),
    owned: scope.createdTabIds.has(43),
    open: tabs.has(43),
  }})}}`);
}}
scope.scopedTabIds.delete(42);
scope.scopedGroupIds.delete(42);
tabs.delete(42);


// A commanded navigation may receive any non-restricted URL without cancelling itself.
tabs.set(23, {{ id: 23, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(23);
scope.scopedGroupIds.set(23, 700);
let expectedNavigationStarted = false;
let releaseExpectedNavigation;
const expectedNavigation = scope.runTabOperation(23, async (lease) => {{
  lease.allowUrlChange();
  expectedNavigationStarted = true;
  await new Promise((resolve) => {{ releaseExpectedNavigation = resolve; }});
  lease.assertCurrent();
  return "navigated";
}});
await waitUntil(() => expectedNavigationStarted);
tabs.get(23).url = "https://after.example";
listeners.updated.forEach((listener) =>
  listener(23, {{ url: "https://after.example" }}),
);
tabs.get(23).url = "https://redirected.example";
listeners.updated.forEach((listener) =>
  listener(23, {{ url: "https://redirected.example" }}),
);
releaseExpectedNavigation();
if ((await settleWithin(expectedNavigation)) !== "navigated") {{
  throw new Error("redirected navigation URL change cancelled its own operation");
}}
if (tabs.get(23).url !== "https://redirected.example") {{
  throw new Error("redirected navigation did not retain its final URL");
}}
await waitUntil(() => !scope.tabOperations.has(23));
scope.scopedTabIds.delete(23);
scope.scopedGroupIds.delete(23);
tabs.delete(23);

// An unrelated operation must still fail when the page URL changes.
tabs.set(24, {{ id: 24, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(24);
scope.scopedGroupIds.set(24, 700);
let unrelatedOperationStarted = false;
const unrelatedOperation = scope.runTabOperation(24, async () => {{
  unrelatedOperationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => unrelatedOperationStarted);
tabs.get(24).url = "https://after.example";
listeners.updated.forEach((listener) =>
  listener(24, {{ url: "https://after.example" }}),
);
const unrelatedOperationError = await settleWithin(unrelatedOperation);
if (unrelatedOperationError?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`unrelated URL change did not cancel active work: ${{unrelatedOperationError?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(24));
scope.scopedTabIds.delete(24);
scope.scopedGroupIds.delete(24);
tabs.delete(24);

// Restricted URLs cancel even an operation that expects a navigation event.
tabs.set(25, {{ id: 25, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(25);
scope.scopedGroupIds.set(25, 700);
let restrictedNavigationStarted = false;
const restrictedNavigation = scope.runTabOperation(25, async (lease) => {{
  lease.allowUrlChange();
  restrictedNavigationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => restrictedNavigationStarted);
tabs.get(25).url = "chrome://settings";
listeners.updated.forEach((listener) =>
  listener(25, {{ url: "chrome://settings" }}),
);
const restrictedNavigationError = await settleWithin(restrictedNavigation);
if (restrictedNavigationError?.code !== ERROR_CODES.RESTRICTED_URL) {{
  throw new Error(`restricted navigation did not cancel active work: ${{restrictedNavigationError?.code}}`);
}}
await waitUntil(() => !scope.scopedTabIds.has(25));
tabs.delete(25);

// A coalesced URL and group event must prioritize group revocation.
tabs.set(26, {{ id: 26, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(26);
scope.scopedGroupIds.set(26, 700);
let coalescedNavigationStarted = false;
const coalescedNavigation = scope.runTabOperation(26, async (lease) => {{
  lease.allowUrlChange();
  coalescedNavigationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => coalescedNavigationStarted);
tabs.get(26).url = "https://after.example";
tabs.get(26).groupId = -1;
listeners.updated.forEach((listener) =>
  listener(26, {{ url: "https://after.example", groupId: -1 }}),
);
const coalescedNavigationError = await settleWithin(coalescedNavigation);
if (coalescedNavigationError?.code !== ERROR_CODES.TAB_NOT_SCOPED) {{
  throw new Error(`coalesced group revocation did not cancel active work: ${{coalescedNavigationError?.code}}`);
}}
await waitUntil(() => !scope.scopedTabIds.has(26));
tabs.delete(26);


// A URL-change grant remains valid for repeated URL events while navigation is in flight.
tabs.set(27, {{ id: 27, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(27);
scope.scopedGroupIds.set(27, 700);
let consumedNavigationStarted = false;
let releaseConsumedNavigation;
const consumedNavigation = scope.runTabOperation(27, async (lease) => {{
  lease.allowUrlChange();
  consumedNavigationStarted = true;
  await new Promise((resolve) => {{ releaseConsumedNavigation = resolve; }});
  lease.assertCurrent();
  return "survived";
}}).then((value) => value, (error) => error);
await waitUntil(() => consumedNavigationStarted);
tabs.get(27).url = "https://after.example";
listeners.updated.forEach((listener) =>
  listener(27, {{ url: "https://after.example" }}),
);
listeners.updated.forEach((listener) =>
  listener(27, {{ url: "https://after.example" }}),
);
releaseConsumedNavigation();
const consumedNavigationOutcome = await settleWithin(consumedNavigation);
if (consumedNavigationOutcome !== "survived") {{
  throw new Error(`repeated URL event cancelled navigation: ${{consumedNavigationOutcome?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(27));
// After the operation ends, an idle URL event has nothing to cancel.
listeners.updated.forEach((listener) =>
  listener(27, {{ url: "https://after.example" }}),
);
await waitUntil(() => !scope.tabOperations.has(27));
// The grant is gone, so the same event cancels a new non-navigation operation.
let postNavigationOperationStarted = false;
const postNavigationOperation = scope.runTabOperation(27, async () => {{
  postNavigationOperationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => postNavigationOperationStarted);
listeners.updated.forEach((listener) =>
  listener(27, {{ url: "https://after.example" }}),
);
const postNavigationError = await settleWithin(postNavigationOperation);
if (postNavigationError?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`post-operation URL event did not cancel: ${{postNavigationError?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(27));
scope.scopedTabIds.delete(27);
scope.scopedGroupIds.delete(27);
tabs.delete(27);

// A URL-change grant accepts a different non-restricted navigation URL.
tabs.set(28, {{ id: 28, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(28);
scope.scopedGroupIds.set(28, 700);
let mismatchedNavigationStarted = false;
let releaseMismatchedNavigation;
const mismatchedNavigation = scope.runTabOperation(28, async (lease) => {{
  lease.allowUrlChange();
  mismatchedNavigationStarted = true;
  await new Promise((resolve) => {{ releaseMismatchedNavigation = resolve; }});
  lease.assertCurrent();
  return "survived";
}}).then((value) => value, (error) => error);
await waitUntil(() => mismatchedNavigationStarted);
tabs.get(28).url = "https://user.example";
listeners.updated.forEach((listener) =>
  listener(28, {{ url: "https://user.example" }}),
);
releaseMismatchedNavigation();
const mismatchedNavigationOutcome = await settleWithin(mismatchedNavigation);
if (mismatchedNavigationOutcome !== "survived") {{
  throw new Error(`different non-restricted URL cancelled navigation: ${{mismatchedNavigationOutcome?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(28));
scope.scopedTabIds.delete(28);
scope.scopedGroupIds.delete(28);
tabs.delete(28);

// Creation completion is required even when the URL already matches.
const emit = (id, change) => {{
  if (tabs.has(id)) Object.assign(tabs.get(id), change);
  listeners.updated.forEach((fn) => fn(id, change));
}};
const added = (id) => scopeEvents.filter((e) => e.event === "scope.tabAdded" && e.params.tabId === id);
const prepareCreate = async (tabId, url = "about:blank", complete = true) => {{
  createTab = undefined;
  const outcome = scope.create({{ url }}).then(() => null, (error) => error);
  await waitUntil(() => typeof createTab === "function");
  tabs.set(tabId, {{ id: tabId, windowId: 2, groupId: -1, url, status: "loading" }});
  createTab({{ ...tabs.get(tabId) }});
  if (complete) emit(tabId, {{ status: "complete" }});
  return {{ outcome }};
}};
scope.operationTimeoutMs = 5000;
// Before identification: URL events and completion must be buffered by tab id.
for (const [id, url, observed, code] of [
  [39, "https://early.example", "https://redirect.example", "COMMAND_TIMEOUT"],
  [40, "https://early.example", "https://early.example/", null],
  [41, "about:blank", "about:blank", null],
]) {{
  createTab = undefined;
  const outcome = scope.create({{ url }}).then(() => null, (e) => e);
  await waitUntil(() => typeof createTab === "function");
  tabs.set(id, {{ id, windowId: 2, groupId: -1, url: observed, status: "loading" }});
  emit(id, {{ url: observed, status: "complete" }});
  createTab({{ ...tabs.get(id) }});
  assert.equal((await settleWithin(outcome))?.code ?? null, code);
  assert.equal(added(id).length, code ? 0 : 1);
  if (!code) await scope.remove({{ tabId: id }});
}}
// URL rewrites during identification, including a combined group update, fail closed.
for (const [id, change] of [
  [42, {{ url: "https://redirect.example" }}],
  [43, {{ url: "about:blank", groupId: 701 }}],
  [44, {{ url: "chrome://settings" }}],
]) {{
  const {{ outcome }} = await prepareCreate(id, "about:blank", false);
  await waitUntil(() => scope.activeCreation?.tabId === id);
  emit(id, change);
  assert.equal((await settleWithin(outcome)).code, "COMMAND_TIMEOUT");
  assert.equal(added(id).length, 0);
  assert(!tabs.has(id));
}}
// A conflicting pending URL or failed final snapshot is rejected even after complete.
for (const [id, finalFailure] of [[45, false], [46, true]]) {{
  const originalGet = chrome.tabs.get;
  let gets = 0;
  chrome.tabs.get = async (tabId) => {{
    const tab = await originalGet(tabId);
    if (tabId === id && (++gets >= (finalFailure ? 2 : 1))) tab.pendingUrl = "https://conflict.example";
    return tab;
  }};
  const {{ outcome }} = await prepareCreate(id);
  assert.equal((await settleWithin(outcome)).code, "COMMAND_TIMEOUT");
  chrome.tabs.get = originalGet;
  assert(!tabs.has(id));
  assert.equal(added(id).length, 0);
}}
// Removal and explicit unshare before the commit cancel creation.
for (const [id, action] of [[47, "remove"], [48, "unshare"], [49, "reset"]]) {{
  const {{ outcome }} = await prepareCreate(id, "about:blank", false);
  await waitUntil(() => scope.activeCreation?.tabId === id);
  if (action === "remove") {{
    tabs.delete(id);
    listeners.removed.forEach((fn) => fn(id));
  }} else if (action === "unshare") {{
    await scope.unshareTab(id).catch(() => undefined);
  }} else {{
    await scope.prepareForReset();
    assert.equal(scope.activeCreation, null);
    await scope.reset();
    scope.finishReset();
  }}
  const error = await settleWithin(outcome);
  assert.equal(error.code, action === "remove" ? "TAB_NOT_FOUND" : "COMMAND_TIMEOUT");
  if (action === "remove") assert.equal(error.message, "The created tab closed during creation.");
  assert.equal(added(id).length, 0);
  assert(!tabs.has(id));
}}
// A closed final snapshot reports the same contract even if onRemoved is delayed.
const originalGetForMissing = chrome.tabs.get;
chrome.tabs.get = async (id) => {{
  if (id === 73) {{ tabs.delete(id); throw new Error("missing tab"); }}
  return originalGetForMissing(id);
}};
const {{ outcome: missingSnapshot }} = await prepareCreate(73);
assert.equal((await settleWithin(missingSnapshot)).message, "The created tab closed during creation.");
chrome.tabs.get = originalGetForMissing;
assert.equal(added(73).length, 0);

// Chrome 153's measured empty-url create reply remains hidden through loading.
createTab = undefined;
const measured = scope.create({{ url: "https://measured.example" }}).then(() => null, (e) => e);
await waitUntil(() => typeof createTab === "function");
tabs.set(61, {{ id: 61, windowId: 2, groupId: -1, url: "", pendingUrl: "https://measured.example", status: "loading" }});
createTab({{ ...tabs.get(61) }});
await waitUntil(() => scope.activeCreation?.tabId === 61);
assert(!scope.isScoped(61));
assert(!(await scope.list()).tabs.some((tab) => tab.tabId === 61));
await assert.rejects(scope.assertScoped(61), {{ code: "TAB_NOT_SCOPED" }});
emit(61, {{ status: "loading", url: "https://measured.example/" }});
await delay(1);
assert.equal(added(61).length, 0);
tabs.get(61).pendingUrl = "";
emit(61, {{ status: "complete" }});
assert.equal(await settleWithin(measured), null);
await scope.remove({{ tabId: 61 }});

// A second creation waits on the stable queue and consumes only its own early events.
const {{ outcome: firstQueued }} = await prepareCreate(62, "about:blank", false);
const firstResolver = createTab;
const secondQueued = scope.create({{}}).then(() => null, (e) => e);
await delay(1);
assert.equal(createTab, firstResolver);
emit(62, {{ status: "complete" }});
assert.equal(await settleWithin(firstQueued), null);
await waitUntil(() => createTab !== firstResolver);
tabs.set(63, {{ id: 63, windowId: 2, groupId: -1, url: "about:blank", status: "complete" }});
emit(63, {{ status: "complete" }});
createTab({{ ...tabs.get(63) }});
assert.equal(await settleWithin(secondQueued), null);
await scope.remove({{ tabId: 62 }});
await scope.remove({{ tabId: 63 }});

// URL changes and unshare while grouping/persisting are still pre-commit failures.
for (const [id, action] of [[64, "url"], [65, "unshare"], [66, "group"]]) {{
  const originalSet = chrome.storage.session.set;
  let injected = false;
  chrome.storage.session.set = async (values) => {{
    if (values.scopedTabGroupIds?.[id] === 700 && !injected) {{
      injected = true;
      assert(!scope.isScoped(id));
      assert.equal(added(id).length, 0);
      if (action === "unshare") await scope.unshareTab(id).catch(() => undefined);
      else emit(id, action === "url" ? {{ url: "https://rewrite.example" }} : {{ groupId: -1 }});
    }}
    return originalSet(values);
  }};
  const {{ outcome }} = await prepareCreate(id);
  assert.equal((await settleWithin(outcome)).code, "COMMAND_TIMEOUT");
  chrome.storage.session.set = originalSet;
  assert(injected && !tabs.has(id) && added(id).length === 0);
}}

// No completion event: the creation's own 3 s deadline, not the operation deadline.
const {{ outcome: neverComplete }} = await prepareCreate(50, "about:blank", false);
assert.equal((await settleWithin(neverComplete, 3500)).message,
  "The created tab did not settle on the requested URL.");
assert(!tabs.has(50));

// Share, group admission and popup inheritance wait outside queues while unidentified.
// The identified creation is refused; unrelated tabs proceed, including a popup.
tabs.set(51, {{ id: 51, windowId: 2, groupId: 700, url: "https://opener.example" }});
scope.scopedTabIds.add(51);
scope.scopedGroupIds.set(51, 700);
createTab = undefined;
const fenced = scope.create({{}}).then(() => null, (e) => e);
await waitUntil(() => typeof createTab === "function");
for (const id of [52, 53, 54, 55]) {{
  tabs.set(id, {{ id, windowId: 2, groupId: id === 54 ? 700 : -1,
    url: "about:blank", status: "loading", openerTabId: 51 }});
}}
const pendingShare = scope.shareTab(52).then(() => null, (e) => e);
const otherShare = scope.shareTab(53);
const otherGroup = scope.handleTabUpdated(54, {{ groupId: 700 }});
const otherPopup = scope.handleTabCreated(tabs.get(55));
const ownGroup = scope.handleTabUpdated(52, {{ groupId: 700 }});
const ownPopup = scope.handleTabCreated(tabs.get(52));
await delay(2);
for (const id of [52, 53, 54, 55]) {{
  assert(!scope.isScoped(id));
  assert(!scope.tabOperations.has(id));
}}
createTab({{ ...tabs.get(52) }});
assert.equal((await pendingShare).message, "The requested tab is still being created.");
await Promise.all([otherShare, otherGroup, otherPopup, ownGroup, ownPopup]);
assert(!scope.isScoped(52));
for (const id of [53, 54, 55]) assert(scope.isScoped(id));
await assert.rejects(scope.shareTab(52), {{ message: "The requested tab is still being created." }});
await scope.handleTabCreated(tabs.get(52));
await scope.handleTabUpdated(52, {{ groupId: 700 }});
assert(!scope.isScoped(52));
emit(52, {{ status: "complete" }});
assert.equal(await settleWithin(fenced), null);
assert(scope.isScoped(52));
assert(!scope.expectedGroupTransitions.has(52));
for (const id of [51, 52, 53, 54, 55]) {{
  await scope.unshareTab(id);
  tabs.delete(id);
}}

// If creation starts during an admission await, retry outside every tab queue.
for (const [id, kind] of [[67, "share"], [68, "group"], [69, "popup"]]) {{
  tabs.set(70, {{ id: 70, windowId: 2, groupId: 700, url: "https://opener.example" }});
  scope.scopedTabIds.add(70);
  scope.scopedGroupIds.set(70, 700);
  tabs.set(id, {{ id, windowId: 2, groupId: kind === "group" ? 700 : -1,
    url: "about:blank", openerTabId: 70 }});
  const originalGet = chrome.tabs.get;
  const originalSet = chrome.storage.session.set;
  let releaseAdmission;
  let delayed = false;
  if (kind === "popup") {{
    chrome.storage.session.set = async (values) => {{
      if (values.createdTabIds?.includes(id) && !delayed) {{
        delayed = true;
        await new Promise((resolve) => {{ releaseAdmission = resolve; }});
      }}
      return originalSet(values);
    }};
  }} else {{
    chrome.tabs.get = async (tabId) => {{
      const tab = await originalGet(tabId);
      if (tabId === id && !delayed) {{
        delayed = true;
        await new Promise((resolve) => {{ releaseAdmission = resolve; }});
      }}
      return tab;
    }};
  }}
  const admission = kind === "share" ? scope.shareTab(id)
    : kind === "group" ? scope.handleTabUpdated(id, {{ groupId: 700 }})
    : scope.handleTabCreated(tabs.get(id));
  await waitUntil(() => typeof releaseAdmission === "function");
  createTab = undefined;
  const creation = scope.create({{}}).then(() => null, (e) => e);
  await waitUntil(() => typeof createTab === "function");
  releaseAdmission();
  await waitUntil(() => !scope.tabOperations.has(id) && !scope.tabOperations.has(70));
  assert(!scope.isScoped(id));
  tabs.set(71, {{ id: 71, windowId: 2, groupId: -1, url: "about:blank", status: "complete" }});
  emit(71, {{ status: "complete" }});
  createTab({{ ...tabs.get(71) }});
  await admission;
  assert(scope.isScoped(id));
  assert.equal(await settleWithin(creation), null);
  chrome.tabs.get = originalGet;
  chrome.storage.session.set = originalSet;
  await scope.remove({{ tabId: 71 }});
  for (const tabId of [id, 70]) {{ await scope.unshareTab(tabId); tabs.delete(tabId); }}
}}

// Deferred admissions belong to the reset generation in which they arrived.
createTab = undefined;
const resetUnidentified = scope.create({{}}).then(() => null, (e) => e);
await waitUntil(() => typeof createTab === "function");
const resetResolver = createTab;
tabs.set(72, {{ id: 72, windowId: 2, groupId: 700, url: "about:blank" }});
const resetShare = scope.shareTab(72).then(() => null, (e) => e);
const resetGroup = scope.handleTabUpdated(72, {{ groupId: 700 }});
await delay(1);
await scope.prepareForReset();
await scope.reset();
scope.finishReset();
assert.equal((await settleWithin(resetShare)).code, "COMMAND_TIMEOUT");
await resetGroup;
assert(!scope.isScoped(72));
assert.equal((await resetUnidentified).code, "COMMAND_TIMEOUT");
resetResolver({{ ...tabs.get(72) }});
await waitUntil(() => !tabs.has(72));

// Restart before tabs.create answers must retain the unidentified admission fence.
createTab = undefined;
const interruptedCreation = scope.create({{ url: "https://requested.example" }}).catch((e) => e);
await waitUntil(() => typeof createTab === "function");
const interruptedLease = scope.activeCreation.lease;
const durableBeforeCallback = structuredClone(sessionState);
clearTimeout(scope.activeCreation.timer);
const recoveryListenerCounts = Object.fromEntries(["created", "removed", "updated"].map((key) => [key, listeners[key].length]));
tabs.set(73, {{ id: 73, windowId: 2, groupId: 700, url: "https://wrong.example", status: "complete" }});
const recoveredEvents = [];
const recoveredScope = new TabScope({{ sendEvent: (...args) => recoveredEvents.push(args) }});
await recoveredScope.initialize();
await recoveredScope.handleTabUpdated(73, {{ groupId: 700 }});
assert(!recoveredScope.isScoped(73), "restart admitted an unresolved creation at the wrong URL");
await assert.rejects(recoveredScope.shareTab(73), {{ code: "COMMAND_TIMEOUT" }});
assert.deepEqual(recoveredEvents, []);
assert.equal(durableBeforeCallback.pendingTabCreation.url, "https://requested.example");
assert(durableBeforeCallback.pendingTabCreation.deadlineMs > Date.now());
// Once its deadline passes, the recovered fence drops without acquiring ownership.
const realNow = Date.now;
Date.now = () => durableBeforeCallback.pendingTabCreation.deadlineMs + 1;
await recoveredScope.shareTab(73);
await recoveredScope.unshareTab(73);
Date.now = realNow;
assert.equal(sessionState.pendingTabCreation, null);
// An already-expired marker also releases admissions during initialization.
sessionState.pendingTabCreation = {{ ...durableBeforeCallback.pendingTabCreation, deadlineMs: Date.now() - 1 }};
const expiredScope = new TabScope({{ sendEvent: () => undefined }});
await expiredScope.initialize();
await expiredScope.shareTab(73);
await expiredScope.unshareTab(73);
assert(tabs.has(73), "recovery must not acquire ownership of an unidentified tab");
// End the simulated old worker only after checking its durable crash snapshot.
interruptedLease.cancel(new Error("simulated worker stopped"));
await interruptedCreation;
tabs.delete(73);
for (const [key, count] of Object.entries(recoveryListenerCounts)) listeners[key].length = count;

// Failed cleanup stays owned and quarantined across worker restart, then reset closes it.
const originalRemoveForCreation = chrome.tabs.remove;
chrome.tabs.remove = async (id) => {{ if (id === 56) throw new Error("close denied"); return originalRemoveForCreation(id); }};
const {{ outcome: failedClose }} = await prepareCreate(56, "about:blank", false);
await waitUntil(() => scope.activeCreation?.tabId === 56);
emit(56, {{ url: "https://wrong.example" }});
assert.equal((await settleWithin(failedClose)).message, "The created tab could not be closed.");
assert(scope.createdTabIds.has(56) && scope.quarantinedTabIds.has(56));
const restart = new TabScope({{ sendEvent: () => undefined }});
await restart.initialize();
await assert.rejects(restart.shareTab(56), {{ code: "COMMAND_TIMEOUT" }});
tabs.get(56).groupId = 700;
await restart.handleTabUpdated(56, {{ groupId: 700 }});
assert(!restart.isScoped(56));
chrome.tabs.remove = originalRemoveForCreation;
await restart.prepareForReset();
await restart.reset();
restart.finishReset();
assert(!tabs.has(56));
scope.createdTabIds.delete(56);
scope.quarantinedTabIds.delete(56);

// A times out unresolved; reset; B starts; A's late result cannot fence or close B.
scope.operationTimeoutMs = 20;
createTab = undefined;
const orphan = scope.create({{}}).then(() => null, (e) => e);
await waitUntil(() => typeof createTab === "function");
const resolveOrphan = createTab;
assert.equal((await settleWithin(orphan)).code, "COMMAND_TIMEOUT");
assert.equal(scope.activeCreation, null);
await scope.prepareForReset();
await scope.reset();
scope.finishReset();
scope.operationTimeoutMs = 5000;
const {{ outcome: nextCreation }} = await prepareCreate(57, "about:blank", false);
await waitUntil(() => scope.activeCreation?.tabId === 57);
tabs.set(58, {{ id: 58, windowId: 2, groupId: -1, url: "about:blank" }});
resolveOrphan({{ ...tabs.get(58) }});
await waitUntil(() => !tabs.has(58) && !scope.creationCleanups.has(58));
assert.equal(scope.activeCreation.tabId, 57);
emit(57, {{ status: "complete" }});
assert.equal(await settleWithin(nextCreation), null);
await scope.remove({{ tabId: 57 }});

// Another path already scoped the late result: ownership is handed back, never closed.
scope.operationTimeoutMs = 20;
createTab = undefined;
const superseded = scope.create({{}}).then(() => null, (e) => e);
await waitUntil(() => typeof createTab === "function");
const resolveSuperseded = createTab;
await settleWithin(superseded);
tabs.set(59, {{ id: 59, windowId: 2, groupId: 700, url: "about:blank" }});
await scope.shareTab(59);
await scope.unshareTab(59);
resolveSuperseded({{ ...tabs.get(59) }});
await delay(2);
assert(tabs.has(59), "late creation cleanup closed a handed-back tab");
assert(!scope.isScoped(59) && !scope.createdTabIds.has(59) && !scope.isQuarantined(59));
tabs.delete(59);

// Normal explicit-URL creation; an identical URL replay after commit still cancels.
scope.operationTimeoutMs = 5000;
const {{ outcome: successfulCreate }} = await prepareCreate(30, "https://created.example");
assert.equal(await settleWithin(successfulCreate), null);
let replayOperationStarted = false;
const replayOperation = scope.runTabOperation(30, async () => {{
  replayOperationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => replayOperationStarted);
emit(30, {{ url: "https://created.example" }});
assert.equal((await settleWithin(replayOperation)).message,
  "The page changed while the extension operation was running.");
scope.operationTimeoutMs = 20;

// Page.navigate remains available after publication and consumes its own grant.
const creationRouter = new DebuggerRouter({{
  tabScope: scope,
  sendEvent: () => undefined,
  onAttachedChange: () => undefined,
  commandTimeoutMs: 5,
  recoveryTimeoutMs: 5,
  operationDeadlineMarginMs: 1,
}});
creationRouter.attachedTabs.add(30);
creationRouter.attachStates.set(30, {{ status: "attached" }});
chrome.debugger.sendCommand = async (target, method, params) => {{
  if (method === "Page.navigate") {{
    tabs.get(target.tabId).url = params.url;
    listeners.updated.forEach((listener) => listener(target.tabId, {{ url: params.url }}));
  }}
  return {{ method }};
}};
const pageNavigateResult = await creationRouter.send({{
  tabId: 30,
  method: "Page.navigate",
  params: {{ url: "https://created-next.example" }},
}});
if (pageNavigateResult.result.method !== "Page.navigate") {{
  throw new Error("Page.navigate did not work after explicit-URL create");
}}

// Omitting url uses about:blank, which is an allowed creation URL.
const {{ outcome: blankCreate }} = await prepareCreate(33);
const blankCreateOutcome = await settleWithin(blankCreate);
const blankCreateEvents = scopeEvents.filter(
  (entry) => entry.event === "scope.tabAdded" && entry.params?.tabId === 33,
);
if (
  blankCreateOutcome !== null ||
  blankCreateEvents.length !== 1 ||
  blankCreateEvents[0].params.origin !== "created" ||
  blankCreateEvents[0].params.url !== "about:blank"
) {{
  throw new Error(`about:blank create failed: ${{JSON.stringify({{
    outcome: blankCreateOutcome?.code ?? blankCreateOutcome,
    events: blankCreateEvents,
  }})}}`);
}}
tabs.delete(30);
listeners.removed.forEach((listener) => listener(30));
tabs.delete(33);
listeners.removed.forEach((listener) => listener(33));
await waitUntil(() => !scope.scopedTabIds.has(30) && !scope.scopedTabIds.has(33));

// User revocation must invalidate an active operation before the queued update handler runs.
tabs.set(20, {{ id: 20, windowId: 1, groupId: 700, url: "https://revoked.example" }});
scope.scopedTabIds.add(20);
scope.scopedGroupIds.set(20, 700);
const originalRemoveFromScopeLocked = scope.removeFromScopeLocked.bind(scope);
let revocationCleanupStarted = false;
let releaseRevocationCleanup;
scope.removeFromScopeLocked = async (...args) => {{
  if (args[0] === 20) {{
    revocationCleanupStarted = true;
    await new Promise((resolve) => {{ releaseRevocationCleanup = resolve; }});
  }}
  return originalRemoveFromScopeLocked(...args);
}};
let operationStarted = false;
const revokedOperation = scope.runTabOperation(20, async () => {{
  operationStarted = true;
  return new Promise(() => undefined);
}}).then(() => null, (error) => error);
await waitUntil(() => operationStarted);
tabs.get(20).groupId = -1;
listeners.updated.forEach((listener) => listener(20, {{ groupId: -1 }}));
const immediateRevocationError = await settleWithin(revokedOperation);
if (immediateRevocationError?.code !== ERROR_CODES.TAB_NOT_SCOPED) {{
  throw new Error(`group revocation did not invalidate active work: ${{immediateRevocationError?.code}}`);
}}
await waitUntil(() => revocationCleanupStarted);
listeners.updated.forEach((listener) =>
  listener(20, {{ url: "https://revoked.example/next" }}),
);
releaseRevocationCleanup();
await waitUntil(() => !scope.scopedTabIds.has(20));
if (scope.tabOperationLeases.has(20)) {{
  throw new Error("revoked tab retained an operation lease");
}}
scope.removeFromScopeLocked = originalRemoveFromScopeLocked;
tabs.delete(20);
// Restored scope metadata must fail closed when it references no real group.
tabs.set(14, {{ id: 14, windowId: 1, groupId: -1, url: "https://restore.example" }});
sessionState.scopedTabIds = [14];
sessionState.scopedTabGroupIds = {{ "14": -1 }};
const restoredScope = new TabScope({{ sendEvent: () => undefined, operationTimeoutMs: 20 }});
await restoredScope.initialize();
if (restoredScope.scopedTabIds.has(14)) {{
  throw new Error("invalid restored group metadata retained tab scope");
}}
sessionState.scopedTabIds = [];
sessionState.scopedTabGroupIds = {{}};
tabs.delete(14);
// A stale positive group ID must not adopt a different controlled group.
tabs.set(16, {{ id: 16, windowId: 1, groupId: 701, url: "https://stale-group.example" }});
sessionState.scopedTabIds = [16];
sessionState.scopedTabGroupIds = {{ "16": 700 }};
const staleGroupScope = new TabScope({{ sendEvent: () => undefined, operationTimeoutMs: 20 }});
await staleGroupScope.initialize();
if (staleGroupScope.scopedTabIds.has(16)) {{
  throw new Error("stale restored group ID adopted a different group");
}}
sessionState.scopedTabIds = [];
sessionState.scopedTabGroupIds = {{}};
tabs.delete(16);

// Collection must revoke a tab when the controlled group is renamed.
tabs.set(17, {{ id: 17, windowId: 1, groupId: 700, url: "https://renamed-group.example" }});
scope.scopedTabIds.add(17);
scope.scopedGroupIds.set(17, 700);
const originalGroupGet = chrome.tabGroups.get;
chrome.tabGroups.get = async (groupId) => ({{ id: groupId, title: "Renamed" }});
const renamedTabs = await scope.collectScopedTabs(false);
chrome.tabGroups.get = originalGroupGet;
if (renamedTabs.length !== 0 || scope.scopedTabIds.has(17)) {{
  throw new Error("renamed group remained observable");
}}
tabs.delete(17);
// Popup inheritance must reject an opener whose controlled group was renamed.
tabs.set(18, {{ id: 18, windowId: 1, groupId: 700, url: "https://opener.example" }});
tabs.set(19, {{ id: 19, openerTabId: 18, windowId: 1, groupId: -1, url: "https://child.example" }});
scope.scopedTabIds.add(18);
scope.scopedGroupIds.set(18, 700);
chrome.tabGroups.get = async (groupId) => ({{ id: groupId, title: "Renamed" }});
await scope.handleTabCreated(tabs.get(19));
chrome.tabGroups.get = originalGroupGet;
if (
  scope.scopedTabIds.has(18) ||
  scope.scopedTabIds.has(19) ||
  scope.createdTabIds.has(19)
) {{
  throw new Error("popup inherited scope from a revoked opener");
}}
tabs.delete(18);
tabs.delete(19);

// Group creation must roll back when Chrome cannot label Skyvern Controlled.
tabs.set(15, {{ id: 15, windowId: 1, groupId: -1, url: "https://group-failure.example" }});
const originalGroupUpdate = chrome.tabGroups.update;
chrome.tabGroups.update = async () => {{ throw new Error("group update failed"); }};
const groupFailure = await scope.shareTab(15).then(() => null, (error) => error);
chrome.tabGroups.update = originalGroupUpdate;
if (
  groupFailure?.code !== ERROR_CODES.INTERNAL ||
  scope.scopedTabIds.has(15) ||
  tabs.get(15)?.groupId !== -1
) {{
  throw new Error(`group setup failed open: code=${{groupFailure?.code}} scoped=${{scope.scopedTabIds.has(15)}} group=${{tabs.get(15)?.groupId}}`);
}}
tabs.delete(15);
const originalRemove = chrome.tabs.remove;
let releaseRemove;
chrome.tabs.remove = (tabId) => {{
  if (tabId === 13) {{
    return new Promise((resolve) => {{
      releaseRemove = () => {{
        tabs.delete(tabId);
        resolve();
      }};
    }});
  }}
  return originalRemove(tabId);
}};

// Reset must invalidate an in-flight create before ACK and the late Chrome result
// must never scope a tab into the new epoch.
createTab = undefined;
const createOutcome = scope.create({{ url: "https://old-client.example" }}).then(
  () => null,
  (error) => error,
);
await waitUntil(() => typeof createTab === "function");
await settleWithin(scope.prepareForReset());
await scope.reset();
scope.finishReset();
tabs.set(10, {{ id: 10, windowId: 1, groupId: -1, url: "https://old-client.example" }});
createTab(tabs.get(10));
const createError = await settleWithin(createOutcome);
if (createError?.code !== ERROR_CODES.COMMAND_TIMEOUT || scope.scopedTabIds.has(10)) {{
  throw new Error(`late create crossed reset: ${{createError?.code}} scoped=${{scope.scopedTabIds.has(10)}}`);
}}

// Reset must quarantine an in-flight removal until the late Chrome result is
// reconciled, so the next ownership epoch cannot re-share the tab.
tabs.set(13, {{ id: 13, windowId: 1, groupId: 700, url: "https://remove.example" }});
scope.scopedTabIds.add(13);
scope.scopedGroupIds.set(13, 700);
const removeOutcome = scope.remove({{ tabId: 13 }}).then(
  () => null,
  (error) => error,
);
await waitUntil(() => typeof releaseRemove === "function");
await settleWithin(scope.prepareForReset());
await scope.reset();
scope.finishReset();
if (!scope.quarantinedTabIds.has(13)) {{
  throw new Error("removal was not quarantined before reset");
}}
const shareOutcome = scope.shareTab(13).then(
  () => null,
  (error) => error,
);
const shareError = await settleWithin(shareOutcome);
if (
  shareError?.code !== ERROR_CODES.COMMAND_TIMEOUT ||
  scope.scopedTabIds.has(13)
) {{
  throw new Error(`removal crossed reset: ${{shareError?.code}} scoped=${{scope.scopedTabIds.has(13)}}`);
}}
releaseRemove();
await settleWithin(removeOutcome);
await waitUntil(() => !scope.quarantinedTabIds.has(13));

// A permanently unresolved scoped operation must not strand resetFinished.
const wedgedOutcome = scope.runTabOperation(99, () => new Promise(() => undefined)).then(
  () => null,
  (error) => error,
);
await waitUntil(() => scope.activeOperationCount === 1);
await settleWithin(scope.prepareForReset());
await scope.reset();
scope.finishReset();
const wedgedError = await settleWithin(wedgedOutcome);
if (wedgedError?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`wedged operation was not cancelled by reset: ${{wedgedError?.code}}`);
}}

for (const tabId of [11, 12, 21]) {{
  tabs.set(tabId, {{ id: tabId, windowId: 1, groupId: 700, url: "https://example.test" }});
  scope.scopedTabIds.add(tabId);
  scope.scopedGroupIds.set(tabId, 700);
}}
const routerEvents = [];
const router = new DebuggerRouter({{
  tabScope: scope,
  sendEvent: (event, params) => routerEvents.push({{ event, params }}),
  onAttachedChange: () => undefined,
  attachTimeoutMs: 5,
  commandTimeoutMs: 5,
  recoveryTimeoutMs: 5,
  operationDeadlineMarginMs: 1,
}});

scope.setDebuggerRouter(router);
chrome.debugger.detach = async ({{ tabId }}) => {{
  debuggerAttached.delete(tabId);
}};
const markAttached = (tabId, url) => {{
  tabs.set(tabId, {{ id: tabId, windowId: 1, groupId: 700, url }});
  scope.scopedTabIds.add(tabId);
  scope.scopedGroupIds.set(tabId, 700);
  router.attachedTabs.add(tabId);
  router.attachStates.set(tabId, {{ status: "attached" }});
  debuggerAttached.add(tabId);
}};

// Page.navigate accepts its only non-restricted committed URL event.
markAttached(34, "https://before.example");
let releaseRedirectNavigation;
let redirectNavigationStarted = false;
chrome.debugger.sendCommand = async (target, method, params) => {{
  if (target.tabId === 34 && method === "Page.navigate") {{
    redirectNavigationStarted = true;
    tabs.get(34).url = "https://redirected.example";
    listeners.updated.forEach((listener) =>
      listener(34, {{ url: "https://redirected.example" }}),
    );
    await new Promise((resolve) => {{ releaseRedirectNavigation = resolve; }});
  }}
  return {{ method }};
}};
const redirectNavigation = router.send({{
  tabId: 34,
  method: "Page.navigate",
  params: {{ url: "https://requested.example" }},
}}).then((value) => value, (error) => error);
await waitUntil(() => redirectNavigationStarted);
releaseRedirectNavigation();
const redirectNavigationOutcome = await settleWithin(redirectNavigation);
if (
  redirectNavigationOutcome?.result?.method !== "Page.navigate" ||
  !router.attachedTabs.has(34) ||
  !debuggerAttached.has(34) ||
  !scope.scopedTabIds.has(34) ||
  tabs.get(34)?.url !== "https://redirected.example"
) {{
  throw new Error(`redirected navigation failed: ${{JSON.stringify({{
    outcome: redirectNavigationOutcome?.code ?? redirectNavigationOutcome,
    attached: router.attachedTabs.has(34),
    debuggerAttached: debuggerAttached.has(34),
    scoped: scope.scopedTabIds.has(34),
    url: tabs.get(34)?.url,
  }})}}`);
}}
const tab34DetachEvents = routerEvents.filter(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 34,
);
const tab34RemovedEvents = scopeEvents.filter(
  (entry) => entry.event === "scope.tabRemoved" && entry.params.tabId === 34,
);
if (
  tab34DetachEvents.length !== 0 ||
  tab34RemovedEvents.length !== 0 ||
  router.attachStates.get(34)?.status === "quarantined"
) {{
  throw new Error(`accepted navigation generated cleanup events: ${{JSON.stringify({{
    detach: tab34DetachEvents,
    removed: tab34RemovedEvents,
    state: router.attachStates.get(34),
  }})}}`);
}}

// Reload and history navigation accept a different non-restricted committed URL.
markAttached(36, "https://reload-before.example");
chrome.debugger.sendCommand = async (target, method) => {{
  if (target.tabId === 36 && method === "Page.reload") {{
    tabs.get(36).url = "https://reload-after.example";
    listeners.updated.forEach((listener) =>
      listener(36, {{ url: "https://reload-after.example" }}),
    );
  }} else if (target.tabId === 36 && method === "Page.navigateToHistoryEntry") {{
    tabs.get(36).url = "https://history-after.example";
    listeners.updated.forEach((listener) =>
      listener(36, {{ url: "https://history-after.example" }}),
    );
  }}
  return {{ method }};
}};
for (const [method, params, expectedUrl] of [
  ["Page.reload", {{}}, "https://reload-after.example"],
  ["Page.navigateToHistoryEntry", {{ entryId: 1 }}, "https://history-after.example"],
]) {{
  const navigation = await router.send({{ tabId: 36, method, params }});
  if (
    navigation.result.method !== method ||
    tabs.get(36)?.url !== expectedUrl ||
    !router.attachedTabs.has(36) ||
    !debuggerAttached.has(36) ||
    !scope.scopedTabIds.has(36)
  ) {{
    throw new Error(`${{method}} did not accept its committed URL: ${{JSON.stringify({{
      navigation,
      url: tabs.get(36)?.url,
      attached: router.attachedTabs.has(36),
      debuggerAttached: debuggerAttached.has(36),
      scoped: scope.scopedTabIds.has(36),
    }})}}`);
  }}
}}
tabs.delete(36);
scope.scopedTabIds.delete(36);
scope.scopedGroupIds.delete(36);
router.attachedTabs.delete(36);
router.attachStates.delete(36);
debuggerAttached.delete(36);

// A restricted committed URL cancels with RESTRICTED_URL and detaches the debugger.
markAttached(35, "https://before.example");
chrome.debugger.sendCommand = async (target, method, params) => {{
  if (target.tabId === 35 && method === "Page.navigate") {{
    tabs.get(35).url = "chrome://settings";
    listeners.updated.forEach((listener) =>
      listener(35, {{ url: "chrome://settings" }}),
    );
  }}
  return {{ method }};
}};
const restrictedRedirect = router.send({{
  tabId: 35,
  method: "Page.navigate",
  params: {{ url: "https://requested.example" }},
}}).then((value) => value, (error) => error);
const restrictedRedirectOutcome = await settleWithin(restrictedRedirect);
await waitUntil(() => !scope.scopedTabIds.has(35) && !router.attachedTabs.has(35));
if (
  restrictedRedirectOutcome?.code !== ERROR_CODES.RESTRICTED_URL ||
  router.attachedTabs.has(35) ||
  debuggerAttached.has(35)
) {{
  throw new Error(`restricted redirect did not detach: ${{restrictedRedirectOutcome?.code}}`);
}}
const restrictedDetachEvents = routerEvents.filter(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 35,
);
const restrictedRemovedEvents = scopeEvents.filter(
  (entry) => entry.event === "scope.tabRemoved" && entry.params.tabId === 35,
);
if (
  restrictedDetachEvents.length !== 1 ||
  restrictedDetachEvents[0].params.reason !== "controllability_lost" ||
  restrictedRemovedEvents.length !== 1
) {{
  throw new Error(`restricted navigation cleanup was wrong: ${{JSON.stringify({{
    detach: restrictedDetachEvents,
    removed: restrictedRemovedEvents,
  }})}}`);
}}
tabs.delete(35);

// Queued debugger leases report before-start; invoked commands report running.
for (const method of ["Runtime.evaluate", "Input.dispatchMouseEvent"]) {{
  markAttached(60, "https://before.example");
  let finishLate;
  let invocations = 0;
  chrome.debugger.sendCommand = () => {{
    invocations += 1;
    return new Promise((resolve) => {{ finishLate = resolve; }});
  }};
  const running = router.send({{ tabId: 60, method, params: {{}} }}).then(() => null, (e) => e);
  await waitUntil(() => typeof finishLate === "function");
  const queued = router.send({{ tabId: 60, method: "Input.dispatchMouseEvent" }}).then(() => null, (e) => e);
  emit(60, {{ url: "https://changed.example" }});
  assert.equal((await settleWithin(running)).message, "The page changed while the extension operation was running.");
  assert.equal((await settleWithin(queued)).message, "The page changed before the extension operation started.");
  assert.equal(invocations, 1);
  finishLate({{ stale: true }});
  await delay(1);
  assert(router.attachedTabs.has(60) && scope.isScoped(60));
  chrome.debugger.sendCommand = async () => ({{ fresh: true }});
  assert.deepEqual((await router.send({{ tabId: 60, method: "Page.getFrameTree" }})).result, {{ fresh: true }});
  await scope.unshareTab(60);
  tabs.delete(60);
}}

// Exempt commands retain their per-tab order and Chrome results across URL events.
for (const restricted of [false, true]) {{
  markAttached(60, "https://before.example");
  const calls = [];
  let finishEnable;
  chrome.debugger.sendCommand = (_target, method) => {{
    calls.push(method);
    return method === "Page.enable"
      ? new Promise((resolve) => {{ finishEnable = resolve; }})
      : Promise.resolve({{ method, fromChrome: true }});
  }};
  const enable = router.send({{ tabId: 60, method: "Page.enable" }}).catch((e) => e);
  await waitUntil(() => finishEnable);
  const tree = router.send({{ tabId: 60, method: "Page.getFrameTree" }}).catch((e) => e);
  const bootstrap = router.send({{ tabId: 60, method: "Runtime.evaluate", params: {{
    expression: "(() => {{\\n const module = {{}};\\n return new (module.exports.UtilityScript())(globalThis, false);\\n }})();",
    contextId: 1,
  }} }}).catch((e) => e);
  emit(60, {{ url: restricted ? "chrome://settings" : "https://changed.example" }});
  finishEnable({{ enabledByChrome: true }});
  if (restricted) {{
    for (const result of await Promise.all([enable, tree, bootstrap])) assert.equal(result.code, "RESTRICTED_URL");
    assert.deepEqual(calls, ["Page.enable"]);
    await waitUntil(() => !scope.isScoped(60));
  }} else {{
    assert.deepEqual((await enable).result, {{ enabledByChrome: true }});
    assert.deepEqual((await tree).result, {{ method: "Page.getFrameTree", fromChrome: true }});
    assert.deepEqual((await bootstrap).result, {{ method: "Runtime.evaluate", fromChrome: true }});
    assert.deepEqual(calls, ["Page.enable", "Page.getFrameTree", "Runtime.evaluate"]);
    await scope.unshareTab(60);
  }}
  tabs.delete(60);
}}

// A non-navigation operation is cancelled by a URL event, but cancellation does not detach the debugger.
markAttached(37, "https://before.example");
let nonNavigationStarted = false;
chrome.debugger.sendCommand = (target, method, params) => {{
  if (
    target.tabId === 37 &&
    method === "Runtime.evaluate" &&
    params.expression === "cancel-me"
  ) {{
    nonNavigationStarted = true;
    return new Promise(() => undefined);
  }}
  return Promise.resolve({{ method }});
}};
const nonNavigation = router.send({{
  tabId: 37,
  method: "Runtime.evaluate",
  params: {{ expression: "cancel-me" }},
}}).then((value) => value, (error) => error);
await waitUntil(() => nonNavigationStarted);
tabs.get(37).url = "https://changed.example";
listeners.updated.forEach((listener) =>
  listener(37, {{ url: "https://changed.example" }}),
);
const nonNavigationOutcome = await settleWithin(nonNavigation);
if (
  nonNavigationOutcome?.code !== ERROR_CODES.COMMAND_TIMEOUT ||
  !router.attachedTabs.has(37) ||
  !scope.scopedTabIds.has(37) ||
  router.attachStates.get(37)?.status !== "attached"
) {{
  throw new Error(`non-navigation cancellation detached or failed: ${{nonNavigationOutcome?.code}}`);
}}
if (
  routerEvents.some((entry) =>
    entry.event === "debugger.detached" && entry.params.tabId === 37
  ) ||
  scopeEvents.some((entry) =>
    entry.event === "scope.tabRemoved" && entry.params.tabId === 37
  ) ||
  router.attachStates.get(37)?.status === "quarantined"
) {{
  throw new Error("non-navigation lease cancellation recovered or removed the tab");
}}
await waitUntil(() => !scope.tabOperations.has(37));
const afterCancellation = await router.send({{
  tabId: 37,
  method: "Runtime.evaluate",
  params: {{ expression: "2+2" }},
}});
if (afterCancellation.result.method !== "Runtime.evaluate") {{
  throw new Error("next command did not succeed after lease cancellation");
}}
tabs.delete(34);
tabs.delete(37);
scope.scopedTabIds.delete(34);
scope.scopedGroupIds.delete(34);
scope.scopedTabIds.delete(37);
scope.scopedGroupIds.delete(37);

// Queued callers share the failed attach state. A late successful attach is
// quarantined and detached before a later caller can retry.
let resolveLateAttach;
let attachCalls = 0;
chrome.debugger.attach = ({{ tabId }}) => {{
  attachCalls += 1;
  if (tabId !== 11) {{
    debuggerAttached.add(tabId);
    return Promise.resolve();
  }}
  return new Promise((resolve) => {{
    resolveLateAttach = () => {{ debuggerAttached.add(tabId); resolve(); }};
  }});
}};
const firstAttach = router.attach({{ tabId: 11 }}).then(() => null, (error) => error);
await waitUntil(() => router.attachStates.get(11)?.status === "attaching");
const queuedAttach = router.attach({{ tabId: 11 }}).then(() => null, (error) => error);
const firstAttachError = await settleWithin(firstAttach);
const queuedAttachError = await settleWithin(queuedAttach);
if (
  firstAttachError?.code !== ERROR_CODES.ATTACH_FAILED ||
  queuedAttachError?.code !== ERROR_CODES.ATTACH_FAILED ||
  attachCalls !== 1
) {{
  throw new Error(`attach retry storm: calls=${{attachCalls}} first=${{firstAttachError?.code}} queued=${{queuedAttachError?.code}}`);
}}
const detachEvents = [];
chrome.debugger.detach = async ({{ tabId }}) => {{
  detachEvents.push(tabId);
  debuggerAttached.delete(tabId);
}};
resolveLateAttach();
await waitUntil(() => detachEvents.includes(11) && !router.attachStates.has(11));
if (debuggerAttached.has(11) || router.attachedTabs.has(11)) {{
  throw new Error("late attach left a zombie debugger session");
}}

// Browser-originated detach is the only source of its Chrome reason, and a
// later internal cleanup must not notify the bridge a second time.
markAttached(38, "https://browser-detach.example");
listeners.debuggerDetach.forEach((listener) => listener({{ tabId: 38 }}, "canceled_by_user"));
await waitUntil(() => routerEvents.some(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 38,
));
await router.detachIfAttached(38);
const browserDetachEvents = routerEvents.filter(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 38,
);
if (
  browserDetachEvents.length !== 1 ||
  browserDetachEvents[0].params.reason !== "canceled_by_user"
) {{
  throw new Error(`browser detach notification was duplicated or changed: ${{JSON.stringify(browserDetachEvents)}}`);
}}
router.clearDetachedState(39, null, "reset");
if (routerEvents.some(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 39,
)) {{
  throw new Error("clearing an unattached tab emitted a detach notification");
}}

// A detach callback that arrives after the timeout must reconcile local state.
await router.attach({{ tabId: 12 }});
let resolveLateDetach;
chrome.debugger.detach = ({{ tabId }}) => new Promise((resolve) => {{
  resolveLateDetach = () => {{ debuggerAttached.delete(tabId); resolve(); }};
}});
const detachError = await settleWithin(
  router.detach({{ tabId: 12 }}).then(() => null, (error) => error),
);
if (detachError?.code !== ERROR_CODES.DEBUGGER_DETACHED) {{
  throw new Error(`late detach did not return DEBUGGER_DETACHED: ${{detachError?.code}}`);
}}
resolveLateDetach();
await waitUntil(() => !router.attachedTabs.has(12) && !router.attachStates.has(12));

router.attachStates.set(77, {{ status: "failed", reason: "old failure" }});
await router.reset();
if (router.attachStates.has(77)) {{
  throw new Error("reset retained a failed attach state");
}}

// The command, timeout recovery, and response all stay inside the genuine
// per-tab queue. A queued same-tab operation cannot delay recovery past the
// bridge's outer response deadline.
scope.scopedTabIds.add(21);
router.attachedTabs.add(21);
router.attachStates.set(21, {{ status: "attached" }});
debuggerAttached.add(21);
const order = [];
chrome.debugger.sendCommand = () => {{
  order.push("command-start");
  return new Promise(() => undefined);
}};
let resolveRecoveryDetach;
chrome.debugger.detach = ({{ tabId }}) => {{
  order.push("detach-start");
  return new Promise((resolve) => {{
    resolveRecoveryDetach = () => {{
      debuggerAttached.delete(tabId);
      order.push("detach-late-done");
      resolve();
    }};
  }});
}};
const frames = [];
const bridge = new BridgeConnection({{
  onRequest: (op, args) => op === "debugger.send" ? router.send(args) : Promise.resolve({{}}),
  onAuthenticated: async () => undefined,
  onReset: async () => ({{ executed: true, ok: true, failedTabCount: 0 }}),
  onStateChange: () => undefined,
  requestTimeoutMs: 40,
}});
bridge.authenticated = true;
const socket = {{ readyState: WebSocket.OPEN, send: (raw) => {{ order.push("response"); frames.push(JSON.parse(raw)); }} }};
bridge.socket = socket;
bridge.connectionGeneration = 3;
let releasePriorOperation;
const priorOperation = scope.runTabOperation(21, () => {{
  order.push("prior-start");
  return new Promise((resolve) => {{ releasePriorOperation = resolve; }});
}});
await waitUntil(() => order.includes("prior-start"));
bridge.enqueueIncomingMessage(
  JSON.stringify({{
    v: 2,
    type: "request",
    id: "ordered-timeout",
    op: "debugger.send",
    args: {{ tabId: 21, method: "Page.navigate", params: {{ url: "https://example.test" }} }},
  }}),
  3,
  socket,
);
await delay(2);
releasePriorOperation();
await priorOperation;
await waitUntil(() => order.includes("command-start"));
const queuedOperation = scope.runTabOperation(21, () => {{
  order.push("queued-start");
  return new Promise(() => undefined);
}}).catch(() => undefined);
await waitUntil(() => frames.length === 1);
const frame = frames[0];
if (frame.ok !== false || frame.error?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`unexpected bridge timeout frame: ${{JSON.stringify(frame)}}`);
}}
await waitUntil(() => order.includes("queued-start"));
if (
  router.attachStates.get(21)?.status !== "quarantined" ||
  router.attachedTabs.has(21) ||
  !(order.indexOf("detach-start") < order.indexOf("queued-start") &&
    order.indexOf("detach-start") < order.indexOf("response"))
) {{
  throw new Error(`recovery escaped the tab queue or outer deadline: ${{JSON.stringify(order)}}`);
}}
const quarantineDetachEvents = routerEvents.filter(
  (entry) => entry.event === "debugger.detached" && entry.params.tabId === 21,
);
if (
  quarantineDetachEvents.length !== 1 ||
  quarantineDetachEvents[0].params.reason !== "quarantined"
) {{
  throw new Error(`quarantine notification was wrong: ${{JSON.stringify(quarantineDetachEvents)}}`);
}}
resolveRecoveryDetach();
await settleWithin(queuedOperation);
await waitUntil(() => !router.attachedTabs.has(21) && !router.attachStates.has(21));
"""

    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "case",
    [
        "child_popup",
        "child_panel",
        "child_app",
        "child_devtools",
        "child_normal",
        "startup_match",
        "startup_unscoped",
        "startup_ungroup_failure",
        "startup_ungroup_noop",
        "startup_enumeration_failure",
        "startup_group_lookup_failure",
        "startup_attached",
        "startup_attached_unowned",
        "restore_groupless",
        "normal_groupless",
        "popup_positive",
        "lookup_failure",
        "attached",
        "regroup_scoped",
        "regroup_unscoped",
        "regroup_lookup_failure",
        "create_popup",
        "group_race",
        "admission_validation_race",
    ],
)
def test_popup_window_scope_contract(case: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the extension scope test")
    extension_dir = Path(__file__).parents[3] / "skyvern" / "browser_extension" / "extension"
    script = f"""
import assert from "node:assert/strict";
const tabs = new Map();
const stored = {{}};
const events = [];
const removed = [];
const updates = [];
const commands = [];
const listener = {{ addListener() {{}} }};
globalThis.chrome = {{
  tabs: {{
    onCreated: listener, onRemoved: listener,
    onUpdated: {{ addListener(fn) {{ updates.push(fn); }} }},
    async get(id) {{ if (!tabs.has(id)) throw new Error("missing tab"); return {{ ...tabs.get(id) }}; }},
    async query() {{ return [...tabs.values()]; }},
    async remove(id) {{ removed.push(id); tabs.delete(id); }},
    async create({{ url }}) {{
      const tab = {{ id: 3, windowId: 2, groupId: -1, url, status: "complete" }};
      tabs.set(tab.id, tab);
      updates.forEach((fn) => fn(tab.id, {{ status: "complete" }}));
      return {{ ...tab }};
    }},
  }},
  tabGroups: {{
    async query() {{ return []; }},
    async get(id) {{ return {{ id, title: "Skyvern Controlled" }}; }},
    async update() {{}},
  }},
  storage: {{ session: {{
    async get(defaults) {{ return {{ ...defaults, ...stored }}; }},
    async set(values) {{ Object.assign(stored, structuredClone(values)); }},
    async remove(keys) {{ for (const key of keys) delete stored[key]; }},
  }} }},
  debugger: {{
    onEvent: listener, onDetach: listener,
    async attach() {{}}, async detach() {{}},
    async sendCommand(target, method) {{ commands.push({{ target, method }}); return {{}}; }},
  }},
}};
{_WINDOW_GROUP_FAKES}
const state = installWindowGroupFakes(chrome, tabs, updates);
state.types.set(2, "popup");
const {{ TabScope }} = await import({json.dumps((extension_dir / "tab_scope.js").as_uri())});
const {{ DebuggerRouter }} = await import({json.dumps((extension_dir / "debugger_router.js").as_uri())});
const caseName = {json.dumps(case)};
const tab = {{ id: 2, windowId: 2, groupId: -1, url: "https://popup.example.test" }};
tabs.set(2, tab);
const scope = new TabScope({{ sendEvent: (event, params) => events.push({{ event, params }}) }});
const waitUntil = async (predicate) => {{
  for (let i = 0; i < 100; i++) {{
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }}
  assert.fail("event did not settle");
}};
if (caseName.startsWith("startup_attached")) {{
  state.types.set(2, "normal");
  tab.groupId = 700;
  stored.scopedTabIds = [2];
  stored.scopedTabGroupIds = {{ 2: 700 }};
  stored.createdTabIds = caseName === "startup_attached" ? [2] : [];
  let releaseStorage;
  chrome.storage.session.get = () => new Promise((resolve) => {{ releaseStorage = resolve; }});
  const initializing = scope.initialize();
  tab.windowId = 3;
  state.attached.forEach((fn) => fn(2, {{ newWindowId: 3 }}));
  releaseStorage(structuredClone(stored));
  await initializing;
  await waitUntil(() => events.some((entry) => entry.event === "scope.tabRemoved" && entry.params.reason === "unshared"));
  assert(!scope.isScoped(2));
  assert(!scope.createdTabIds.has(2));
  assert(!stored.scopedTabIds.includes(2));
  assert(!stored.createdTabIds.includes(2));
  await assert.rejects(scope.assertControllableLocked(2), {{ code: "TAB_NOT_SCOPED" }});
}} else if (caseName.startsWith("startup_") || caseName === "restore_groupless") {{
  tab.groupId = caseName === "restore_groupless" ? -1 : 700;
  const unscoped = ["startup_unscoped", "startup_group_lookup_failure"].includes(caseName);
  stored.scopedTabIds = unscoped ? [] : [2];
  stored.createdTabIds = unscoped ? [] : [2];
  stored.scopedTabGroupIds = {{ 2: tab.groupId }};
  // A failed lookup on another restored tab must not abort initialization.
  tabs.set(4, {{ id: 4, windowId: 4, groupId: 700, url: "https://missing.example.test" }});
  stored.scopedTabIds.push(4);
  stored.scopedTabGroupIds[4] = 700;
  state.lookupFailures.add(4);
  state.ungroupFails = caseName === "startup_ungroup_failure";
  state.ungroupNoop = caseName === "startup_ungroup_noop";
  state.enumerationFailure = caseName === "startup_enumeration_failure";
  if (caseName === "startup_group_lookup_failure") {{
    state.groupLookupFailures.add(700);
    state.groupTitles.set(702, "Other group");
    tabs.set(5, {{ ...tab, id: 5, groupId: 702 }});
  }}
  await scope.initialize();
  await scope.ready;
  assert(!scope.isScoped(4));
  if (state.ungroupFails || state.ungroupNoop || state.groupLookupFailures.size) {{
    assert.equal(tab.groupId, 700);
    assert(!scope.isScoped(2));
    assert(!scope.createdTabIds.has(2));
    assert.equal(scope.scopedGroupIds.get(2), undefined);
    assert.equal(stored.scopedTabGroupIds[2], undefined);
    assert(!(await scope.helloTabs()).some((item) => item.tabId === 2));
    assert(!(await scope.list()).tabs.some((item) => item.tabId === 2));
    assert.deepEqual(state.alarms.at(-1), {{ name: "skyvern-popup-group-sweep", options: {{ delayInMinutes: 0.5 }} }});
    const deadline = state.pendingAlarms.get("skyvern-popup-group-sweep").scheduledTime;
    state.now += 10_000;
    await scope.sweepPopupGroups();
    assert.equal(state.pendingAlarms.get("skyvern-popup-group-sweep").scheduledTime, deadline);
    state.ungroupFails = false;
    state.ungroupNoop = false;
    state.groupLookupFailures.clear();
    state.fireAlarm("skyvern-popup-group-sweep");
    await waitUntil(() => tab.groupId === -1 && scope.activeOperationCount === 0);
    assert(!scope.isScoped(2));
    assert(!scope.createdTabIds.has(2));
    assert.equal(scope.scopedGroupIds.get(2), undefined);
    if (caseName === "startup_group_lookup_failure") assert.equal(tabs.get(5).groupId, 702);
  }} else if (state.enumerationFailure) {{
    assert(state.alarms.some((alarm) => alarm.name === "skyvern-popup-group-sweep"));
  }} else {{
    assert.equal(tab.groupId, -1);
    const keepsScope = caseName === "restore_groupless";
    assert.equal(scope.isScoped(2), keepsScope);
    assert.equal(scope.createdTabIds.has(2), keepsScope);
    assert.equal(scope.scopedGroupIds.get(2), keepsScope ? -1 : undefined);
  }}
}} else {{
  await scope.initialize();
  if (caseName.startsWith("child_")) {{
    const type = caseName.slice(6);
    state.types.set(2, type);
    tabs.set(1, {{ id: 1, windowId: 1, groupId: 700, url: "https://opener.example.test" }});
    scope.scopedTabIds.add(1);
    scope.scopedGroupIds.set(1, 700);
    tab.openerTabId = 1;
    // The event snapshot is stale. Grouping must use the live destination.
    await scope.handleTabCreated({{ ...tab, windowId: 1 }});
    assert(scope.isScoped(2));
    assert((await scope.list()).tabs.some((item) => item.tabId === 2));
    assert.equal(tab.windowId, 2);
    assert.equal(tab.groupId, type === "normal" ? 700 : -1);
    assert.equal(state.groupCalls.length, type === "normal" ? 1 : 0);
    if (type === "normal") assert.equal(state.groupCalls[0].createProperties.windowId, 2);
    const router = new DebuggerRouter({{ tabScope: scope, sendEvent() {{}}, onAttachedChange() {{}} }});
    await router.attach({{ tabId: 2 }});
    await router.send({{ tabId: 2, method: "Runtime.evaluate", params: {{ expression: "1" }} }});
    assert(commands.some((command) => command.target.tabId === 2 && command.method === "Runtime.evaluate"));
  }} else if (caseName === "create_popup") {{
    await assert.rejects(scope.create({{ url: "https://created.example.test" }}));
    assert.deepEqual(removed, [3]);
    assert.equal(state.groupCalls.length, 0);
    assert.equal(events.filter((entry) => entry.event === "scope.tabAdded" || entry.event === "tabs.created").length, 0);
  }} else if (caseName === "group_race") {{
    state.types.set(2, "normal");
    state.types.set(3, "normal");
    const group = chrome.tabs.group;
    chrome.tabs.group = async (options) => {{
      const id = await group(options);
      tab.windowId = 3;
      state.attached.forEach((fn) => fn(2, {{ newWindowId: 3 }}));
      return id;
    }};
    await assert.rejects(scope.shareTab(2), {{ code: "TAB_NOT_SCOPED" }});
    await waitUntil(() => scope.activeOperationCount === 0);
    assert.equal(tab.windowId, 3);
    assert.equal(tab.groupId, -1);
    assert(!scope.isScoped(2));
    assert.equal(state.pendingAlarms.size, 0);
  }} else if (caseName === "admission_validation_race") {{
    state.types.set(2, "normal");
    state.types.set(3, "normal");
    const get = chrome.tabs.get;
    let moved = false;
    chrome.tabs.get = async (id) => {{
      // The group record is written after groupTabLocked's own tab validation.
      if (id === 2 && !moved && scope.scopedGroupIds.get(2) === 700) {{
        moved = true;
        tab.windowId = 3;
        state.attached.forEach((fn) => fn(2, {{ newWindowId: 3 }}));
      }}
      return get(id);
    }};
    await assert.rejects(scope.shareTab(2), {{ code: "TAB_NOT_SCOPED" }});
    await waitUntil(() => scope.activeOperationCount === 0);
    assert(moved);
    assert.equal(tab.groupId, -1);
    assert(!scope.isScoped(2));
    assert.equal(scope.scopedGroupIds.get(2), undefined);
    assert.equal(state.pendingAlarms.size, 0);
  }} else {{
    if (caseName !== "regroup_unscoped") {{
      scope.scopedTabIds.add(2);
      scope.scopedGroupIds.set(2, -1);
    }}
    if (caseName === "regroup_lookup_failure") {{
      tab.groupId = 700;
      state.groupLookupFailures.add(700);
      await scope.handleTabUpdated(2, {{ groupId: 700 }});
      assert(!scope.isScoped(2));
      assert.equal(scope.scopedGroupIds.get(2), undefined);
      assert.equal(tab.groupId, 700);
      assert(state.pendingAlarms.has("skyvern-popup-group-sweep"));
      state.groupLookupFailures.clear();
      state.fireAlarm("skyvern-popup-group-sweep");
      await waitUntil(() => scope.activeOperationCount === 0 && tab.groupId === -1);
      assert(!scope.isScoped(2));
    }} else if (caseName.startsWith("regroup_")) {{
      for (const ungroupFails of [false, true]) {{
        if (caseName === "regroup_scoped") {{
          scope.scopedTabIds.add(2);
          scope.scopedGroupIds.set(2, -1);
          scope.createdTabIds.add(2);
        }}
        tab.groupId = 700;
        state.ungroupFails = ungroupFails;
        updates.forEach((fn) => fn(2, {{ groupId: 700 }}));
        await waitUntil(() => !scope.isScoped(2) && (ungroupFails ? state.pendingAlarms.size > 0 : tab.groupId === -1));
        assert(!scope.createdTabIds.has(2));
        assert.equal(scope.scopedGroupIds.get(2), undefined);
        if (ungroupFails) {{
          state.ungroupFails = false;
          state.fireAlarm("skyvern-popup-group-sweep");
        }}
        await waitUntil(() => tab.groupId === -1 && scope.activeOperationCount === 0);
      }}
    }} else if (caseName === "attached") {{
      let lease;
      const running = scope.runTabOperation(2, async (value) => {{ lease = value; await value.invalidated; }});
      const rejected = assert.rejects(running, {{ code: "TAB_NOT_SCOPED" }});
      await waitUntil(() => lease !== undefined);
      tab.windowId = 1;
      state.attached.forEach((fn) => fn(2, {{ newWindowId: 1 }}));
      await rejected;
      await waitUntil(() => events.some((entry) => entry.event === "scope.tabRemoved" && entry.params.reason === "unshared"));
      assert(!scope.isScoped(2));
    }} else {{
      if (caseName === "normal_groupless") state.types.set(2, "normal");
      if (caseName === "popup_positive") {{ tab.groupId = 700; scope.scopedGroupIds.set(2, 700); }}
      if (caseName === "lookup_failure") state.lookupFailures.add(2);
      await assert.rejects(scope.assertControllableLocked(2), {{ code: "TAB_NOT_SCOPED" }});
      assert(!scope.isScoped(2));
      assert.equal(tab.groupId, -1);
    }}
  }}
}}
"""
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script], capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
