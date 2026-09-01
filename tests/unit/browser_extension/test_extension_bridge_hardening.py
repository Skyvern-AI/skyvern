from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


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
      allowUrlChange(expectedUrl = null) {{ allowedUrlChanges.push(expectedUrl); }},
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
if (JSON.stringify(allowedUrlChanges) !== JSON.stringify(["https://example.test/next", null, null])) {{
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
    create() {{ return new Promise((resolve) => {{ createTab = resolve; }}); }},
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
    async group({{ tabIds }}) {{
      const tab = tabs.get(tabIds[0]);
      if (tab) tab.groupId = 700;
      return 700;
    }},
    async ungroup(tabIds) {{
      for (const tabId of tabIds) {{
        const tab = tabs.get(tabId);
        if (tab) tab.groupId = -1;
      }}
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

const scope = new TabScope({{ sendEvent: () => undefined, operationTimeoutMs: 20 }});
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


// A navigation command may cause its own URL event without cancelling itself.
tabs.set(23, {{ id: 23, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(23);
scope.scopedGroupIds.set(23, 700);
let expectedNavigationStarted = false;
let releaseExpectedNavigation;
const expectedNavigation = scope.runTabOperation(23, async (lease) => {{
  lease.allowUrlChange("https://after.example");
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
releaseExpectedNavigation();
if ((await settleWithin(expectedNavigation)) !== "navigated") {{
  throw new Error("expected navigation URL change cancelled its own operation");
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


// A URL-change grant is consumed by one URL event; a replay must cancel, not survive.
tabs.set(27, {{ id: 27, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(27);
scope.scopedGroupIds.set(27, 700);
let consumedNavigationStarted = false;
let releaseConsumedNavigation;
const consumedNavigation = scope.runTabOperation(27, async (lease) => {{
  lease.allowUrlChange("https://after.example");
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
if (consumedNavigationOutcome === "survived") {{
  throw new Error("URL-change grant survived a second URL event instead of being consumed");
}}
if (consumedNavigationOutcome?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`consumed grant replay was not structured: ${{consumedNavigationOutcome?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(27));
scope.scopedTabIds.delete(27);
scope.scopedGroupIds.delete(27);
tabs.delete(27);

// A grant bound to one URL must not spare a navigation to a different URL.
tabs.set(28, {{ id: 28, windowId: 2, groupId: 700, url: "https://before.example" }});
scope.scopedTabIds.add(28);
scope.scopedGroupIds.set(28, 700);
let mismatchedNavigationStarted = false;
let releaseMismatchedNavigation;
const mismatchedNavigation = scope.runTabOperation(28, async (lease) => {{
  lease.allowUrlChange("https://intended.example");
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
if (mismatchedNavigationOutcome === "survived") {{
  throw new Error("URL-change grant spared a navigation it was not bound to");
}}
if (mismatchedNavigationOutcome?.code !== ERROR_CODES.COMMAND_TIMEOUT) {{
  throw new Error(`mismatched URL change was not structured: ${{mismatchedNavigationOutcome?.code}}`);
}}
await waitUntil(() => !scope.tabOperations.has(28));
scope.scopedTabIds.delete(28);
scope.scopedGroupIds.delete(28);
tabs.delete(28);

// Closing a just-created tab must cancel its create operation after Chrome returns the tab id.
const originalAddToScopeLocked = scope.addToScopeLocked.bind(scope);
let addToScopeStarted = false;
let releaseAddToScope;
scope.addToScopeLocked = async (tab, lease) => {{
  addToScopeStarted = true;
  await new Promise((resolve) => {{ releaseAddToScope = resolve; }});
  return originalAddToScopeLocked(tab, lease);
}};
createTab = undefined;
const interruptedCreate = scope.create({{ url: "https://created.example" }}).then(
  () => null,
  (error) => error,
);
await waitUntil(() => typeof createTab === "function");
const createdTab = {{ id: 22, windowId: 2, groupId: -1, url: "https://created.example" }};
tabs.set(22, createdTab);
createTab({{ ...createdTab }});
await waitUntil(() => addToScopeStarted && scope.tabOperationLeases.has(22));
tabs.delete(22);
listeners.removed.forEach((listener) => listener(22));
const interruptedCreateError = await settleWithin(interruptedCreate);
if (interruptedCreateError?.code !== ERROR_CODES.TAB_NOT_FOUND) {{
  throw new Error(`closed created tab did not invalidate create: ${{interruptedCreateError?.code}}`);
}}
releaseAddToScope();
scope.addToScopeLocked = originalAddToScopeLocked;
await waitUntil(() => !scope.tabOperationLeases.has(22));

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
const router = new DebuggerRouter({{
  tabScope: scope,
  sendEvent: () => undefined,
  onAttachedChange: () => undefined,
  attachTimeoutMs: 5,
  commandTimeoutMs: 5,
  recoveryTimeoutMs: 5,
  operationDeadlineMarginMs: 1,
}});

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
