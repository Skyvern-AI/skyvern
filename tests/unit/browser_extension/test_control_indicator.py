from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXTENSION = Path(__file__).parents[3] / "skyvern/browser_extension/extension"


def run_node(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for indicator contract coverage")
    script = script.replace("EXTENSION_URI", json.dumps(EXTENSION.as_uri() + "/"))
    result = subprocess.run(
        [node, "--input-type=module", "--eval", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_indicator_visibility_requires_scope_and_connection_and_binds_query_to_sender() -> None:
    """Disconnects and revocations must hide the indicator; queries cannot read another tab."""
    run_node("""
import assert from 'node:assert/strict';
const { IndicatorState } = await import(EXTENSION_URI + 'indicator_state.js');
const deliveries = [];
const indicator = new IndicatorState({ sendMessage: (id, state) => deliveries.push({ id, ...state }) });
const query = id => indicator.query({ tab: { id }, frameId: 0 });
indicator.onScopeChange(7, true);
assert.equal(query(7).visible, false);
indicator.setConnected(true);
assert.equal(deliveries.at(-1).visible, true);
const connected = query(7);
assert.equal(connected.visible, true);
assert.equal(query(8).visible, false);
assert.equal(indicator.query({ tabId: 7, frameId: 0 }), null);
assert.equal(indicator.query({ tab: { id: 7 }, frameId: 1 }), null);
indicator.onScopeChange(7, false);
assert.equal(query(7).visible, false);
assert.equal(deliveries.at(-1).visible, false);
indicator.onScopeChange(7, true);
indicator.setConnected(false);
assert.equal(query(7).visible, false);
assert.equal(deliveries.at(-1).visible, false);
assert.ok(query(7).revision > connected.revision);
indicator.setConnected(true);
assert.equal(query(7).visible, true);
""")


ROUTER_SETUP = """
import assert from 'node:assert/strict';
const { DebuggerRouter } = await import(EXTENSION_URI + 'debugger_router.js');
const events = [];
let detachListener;
let chromeCommand = async () => ({ data: 'pixels' });
let message = async () => ({ ok: true });
let shown = true;
const lease = { assertCurrent() {}, invalidated: new Promise(() => {}) };
globalThis.chrome = { debugger: {
  onEvent: { addListener() {} },
  onDetach: { addListener(fn) { detachListener = fn; } },
  sendCommand(target, method, params) {
    events.push({ type: 'capture', method });
    return chromeCommand(target, method, params);
  },
} };
const router = new DebuggerRouter({
  tabScope: {
    async runTabOperation(tabId, operation, generation, allow, classify) {
      classify?.(lease);
      return operation(lease);
    },
    async assertControllableLocked() {},
    async handleDebuggerDetachLocked() {},
  },
  sendEvent() {}, onAttachedChange() {},
  isIndicatorVisible: () => shown,
  sendIndicatorMessage(tabId, payload) {
    events.push({ tabId, ...payload });
    return message(payload);
  },
  commandTimeoutMs: 20,
});
router.attachedTabs.add(7);
router.attachStates.set(7, { status: 'attached' });
const send = (method = 'Page.captureScreenshot', extra = {}) => router.send({ tabId: 7, method, ...extra });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
"""


def test_capture_suppression_order_and_best_effort_timeout() -> None:
    """Capture must wait for hide acknowledgment, but a missing script must not block it."""
    run_node(
        ROUTER_SETUP
        + """
let acknowledge;
message = () => new Promise(resolve => { acknowledge = resolve; });
const first = send();
await tick();
assert.equal(events.length, 1);
assert.equal(events[0].type, 'skyvern.indicator.suppress');
acknowledge({ ok: true });
assert.deepEqual(await first, { result: { data: 'pixels' } });
assert.deepEqual(events.map(x => x.type), ['skyvern.indicator.suppress', 'capture', 'skyvern.indicator.release']);
assert.equal(events[0].token, events[2].token);
message = async () => ({ ok: true });
events.length = 0;
await send('Page.startScreencast');
assert.deepEqual(events.map(x => x.type), ['capture']);
events.length = 0;
shown = false;
await send();
assert.deepEqual(events.map(x => x.type), ['capture']);
shown = true;
events.length = 0;
message = () => new Promise(() => {});
await send();
assert.deepEqual(events.map(x => x.type), ['skyvern.indicator.suppress', 'capture', 'skyvern.indicator.release']);
for (const fail of [() => { throw new Error('no script'); }, async () => { throw new Error('no tab'); }]) {
  message = fail;
  assert.deepEqual(await send(), { result: { data: 'pixels' } });
}
console.log('PASS capture waits for suppression acknowledgment and tolerates an unavailable content script');
"""
    )


@pytest.mark.parametrize("terminal", ["resolve", "reject", "detach", "lease", "dispatch_failure"])
def test_capture_release_tracks_underlying_command_settlement(terminal: str) -> None:
    """Timeout/cancellation must keep pending pixels hidden; settlement or detach must release them."""
    run_node(
        ROUTER_SETUP
        + """
const { ProtocolError, ERROR_CODES } = await import(EXTENSION_URI + 'protocol.js');
const terminal = TERMINAL;
const cancellation = new ProtocolError(ERROR_CODES.COMMAND_TIMEOUT, 'lease invalidated');
if (terminal === 'lease') router.commandTimeoutMs = 1000;
let resolveCommand, rejectCommand, cancelLease;
lease.invalidated = new Promise((_, reject) => { cancelLease = reject; });
chromeCommand = () => {
  if (terminal === 'dispatch_failure') throw new Error('Chrome refused the command');
  return new Promise((resolve, reject) => { resolveCommand = resolve; rejectCommand = reject; });
};
// Child-session timeouts leave Chrome running; lease cancellation must do the same.
const pending = send('Page.captureScreenshot', { sessionId: 'child' }).catch(error => error);
if (terminal === 'lease') {
  await tick();
  cancelLease(cancellation);
}
const outcome = await pending;
if (terminal === 'lease') assert.equal(outcome, cancellation);
else assert.equal(outcome.code, terminal === 'dispatch_failure' ? 'CDP_ERROR' : 'COMMAND_TIMEOUT');
if (terminal !== 'dispatch_failure') {
  assert.deepEqual(events.map(x => x.type), ['skyvern.indicator.suppress', 'capture']);
  if (terminal === 'detach') {
    router.tabScope.runTabOperation = () => new Promise(() => {});
    detachListener({ tabId: 7 }, 'canceled_by_user');
    await tick();
    assert.equal(events.at(-1).type, 'skyvern.indicator.release');
    resolveCommand({});
  } else if (terminal === 'reject') {
    rejectCommand(new Error('capture failed'));
  } else {
    resolveCommand({ data: 'late pixels' });
  }
}
await tick();
assert.deepEqual(events.map(x => x.type), ['skyvern.indicator.suppress', 'capture', 'skyvern.indicator.release']);
assert.equal(events[0].token, events[2].token);
assert.equal(router.captureReleases.size, 0);
console.log('PASS suppression survives wrapper cancellation and ends on settlement or detach');
""".replace("TERMINAL", json.dumps(terminal))
    )


CONTENT_SETUP = """
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('control_indicator.js', EXTENSION_URI), 'utf8');
let listener, observer, resolveQuery;
const timers = new Map();
const frames = new Map();
const windowEvents = new Map();
const documentEvents = new Map();
let nextId = 0;
class Element {
  constructor(tag) {
    this.tagName = tag;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.styles = {};
    this.style = { setProperty: (name, value, priority) => { this.styles[name] = { value, priority }; } };
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  append(child) {
    child.remove();
    this.childNodes.push(child);
    child.parentNode = this;
  }
  remove() {
    if (this.parentNode) {
      this.parentNode.childNodes = this.parentNode.childNodes.filter(node => node !== this);
      this.parentNode = null;
    }
  }
  cloneNode(deep = false) {
    const clone = new Element(this.tagName);
    clone.attributes = { ...this.attributes };
    clone.styles = structuredClone(this.styles);
    if (deep) for (const child of this.childNodes) clone.append(child.cloneNode(true));
    return clone;
  }
  attachShadow(options) {
    assert.equal(options.mode, 'closed');
    return new Element('#shadow-root');
  }
}
const document = {
  documentElement: null,
  visibilityState: 'visible',
  createElement: tag => new Element(tag),
  querySelectorAll(tag) {
    const matches = [];
    const visit = node => {
      if (!node) return;
      if (node.tagName === tag) matches.push(node);
      node.childNodes.forEach(visit);
    };
    visit(this.documentElement);
    return matches;
  },
  addEventListener: (name, fn) => documentEvents.set(name, fn),
  removeEventListener: (name, fn) => { if (documentEvents.get(name) === fn) documentEvents.delete(name); },
};
const context = {
  document,
  window: { addEventListener: (name, fn) => windowEvents.set(name, fn) },
  CSSStyleSheet: class { replaceSync(css) { this.css = css; } },
  MutationObserver: class {
    constructor(fn) { observer = fn; }
    observe(target, options) { assert.equal(target, document); assert.equal(options.subtree, true); }
  },
  setTimeout(fn, delay) { const id = ++nextId; timers.set(id, { fn, delay }); return id; },
  clearTimeout(id) { timers.delete(id); },
  requestAnimationFrame(fn) { const id = ++nextId; frames.set(id, fn); return id; },
  cancelAnimationFrame(id) { frames.delete(id); },
  chrome: { runtime: {
    id: 'extension',
    onMessage: { addListener(fn) { listener = fn; } },
    sendMessage(message) {
      assert.equal(typeof listener, 'function', 'listener must precede query');
      assert.equal(message.type, 'skyvern.indicator.query');
      return new Promise(resolve => { resolveQuery = resolve; });
    },
  } },
};
vm.runInNewContext(source, context);
const state = (visible, revision, epoch = 'worker-a') => ({ type: 'skyvern.indicator.state', visible, revision, epoch });
const trusted = { id: 'extension' };
const emit = (message, respond = () => {}) => listener(message, trusted, respond);
const nextFrame = () => {
  const callbacks = [...frames.values()]; frames.clear();
  for (const callback of callbacks) callback();
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
"""


def test_content_script_state_ordering_and_overlapping_suppression() -> None:
    """Stale state and overlapping captures must not expose the indicator in agent images."""
    run_node(
        CONTENT_SETUP
        + """
document.documentElement = new Element('html');
listener(state(true, 1), { id: 'foreign' }, () => {});
assert.equal(document.documentElement.childNodes.length, 0);
emit(state(true, 2));
const host = document.documentElement.childNodes[0];
const display = () => host.styles.display.value;
assert.equal(display(), 'block');
resolveQuery(state(false, 1));
await tick();
assert.equal(display(), 'block', 'stale query must not overwrite a pushed state');
emit(state(false, 3));
emit(state(true, 3));
assert.equal(display(), 'none');
emit(state(true, 0, 'worker-b'));
assert.equal(display(), 'block', 'new worker epoch must supersede old revisions');
let acknowledged = false;
emit({ type: 'skyvern.indicator.suppress', token: 'one' }, () => { acknowledged = true; });
assert.equal(display(), 'none');
nextFrame();
assert.equal(acknowledged, false, 'acknowledgment must wait for paint');
nextFrame();
assert.equal(acknowledged, true);
emit({ type: 'skyvern.indicator.suppress', token: 'two' });
emit(state(true, 1, 'worker-b'));
for (let i = 0; i < 2; i++) emit({ type: 'skyvern.indicator.release', token: 'one' });
assert.equal(display(), 'none', 'release must leave other tokens hidden');
emit({ type: 'skyvern.indicator.release', token: 'two' });
assert.equal(display(), 'block');
windowEvents.get('pageshow')({ persisted: true });
resolveQuery(state(false, 2, 'worker-b'));
await tick();
assert.equal(display(), 'none', 'bfcache restore must refresh scope');
"""
    )


def test_content_script_query_restores_pending_capture_after_navigation() -> None:
    run_node(
        CONTENT_SETUP
        + """
const { DebuggerRouter } = await import(EXTENSION_URI + 'debugger_router.js');
const { IndicatorState } = await import(EXTENSION_URI + 'indicator_state.js');
let finishCapture;
globalThis.chrome = { debugger: {
  onEvent: { addListener() {} }, onDetach: { addListener() {} },
  sendCommand() { return new Promise(resolve => { finishCapture = resolve; }); },
} };
const lease = { assertCurrent() {}, invalidated: new Promise(() => {}) };
const router = new DebuggerRouter({
  tabScope: {
    async runTabOperation(tabId, operation, generation, allow, classify) {
      classify?.(lease);
      return operation(lease);
    },
    async assertControllableLocked() {},
  },
  sendEvent() {}, onAttachedChange() {}, isIndicatorVisible: () => true,
  async sendIndicatorMessage(tabId, message) {
    // Suppression went to the previous document; release reaches the new one.
    if (message.type === 'skyvern.indicator.release') emit(message);
    return { ok: true };
  },
});
router.attachedTabs.add(7);
router.attachStates.set(7, { status: 'attached' });
const indicator = new IndicatorState({
  sendMessage() {}, getCaptureTokens: tabId => router.getCaptureTokens(tabId),
});
indicator.onScopeChange(7, true);
indicator.setConnected(true);
const capture = router.send({ tabId: 7, method: 'Page.captureScreenshot' });
await tick();
assert.equal(typeof finishCapture, 'function');
assert.equal(router.getCaptureTokens(7).length, 1);
assert.deepEqual(router.getCaptureTokens(8), []);
document.documentElement = new Element('html');
resolveQuery(indicator.query({ tab: { id: 7 }, frameId: 0 }));
await tick();
const host = document.documentElement.childNodes[0];
assert.equal(host.styles.display.value, 'none', 'new document must inherit outstanding capture tokens');
assert.equal(timers.size, 1);
assert.equal([...timers.values()][0].delay, 60_000);
finishCapture({ data: 'pixels' });
await capture;
assert.equal(host.styles.display.value, 'block', 'release must clear the inherited capture token');
assert.equal(timers.size, 0);
assert.deepEqual(router.getCaptureTokens(7), []);
console.log('PASS new-document query inherits capture suppression until release');
"""
    )


def test_content_script_remount_removes_cloned_hosts() -> None:
    run_node(
        CONTENT_SETUP
        + """
document.documentElement = new Element('html');
resolveQuery(state(true, 1));
await tick();
const host = document.documentElement.childNodes[0];
const oldRoot = document.documentElement;
document.documentElement = oldRoot.cloneNode(true);
const clonedHost = document.documentElement.childNodes[0];
assert.notEqual(clonedHost, host);
observer();
assert.deepEqual(document.querySelectorAll('skyvern-control-indicator'), [host]);
assert.equal(clonedHost.parentNode, null);
assert.equal(host.parentNode, document.documentElement);
assert.equal(oldRoot.childNodes.length, 0);
observer();
assert.equal(document.querySelectorAll('skyvern-control-indicator').length, 1);
console.log('PASS cloned document root retains exactly one indicator host');
"""
    )
