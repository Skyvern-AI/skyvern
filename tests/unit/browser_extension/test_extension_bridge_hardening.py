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

const tabScope = {{
  async runTabOperation(_tabId, operation) {{
    return operation({{ isCurrent: () => true, assertCurrent() {{}} }});
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
tabs.set(13, {{ id: 13, windowId: 1, groupId: -1, url: "https://remove.example" }});
scope.scopedTabIds.add(13);
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
  tabs.set(tabId, {{ id: tabId, windowId: 1, groupId: -1, url: "https://example.test" }});
  scope.scopedTabIds.add(tabId);
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
