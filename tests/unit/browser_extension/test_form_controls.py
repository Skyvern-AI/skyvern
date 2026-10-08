from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_fixed_form_fill_authorization_and_document_handoff() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for extension message contract coverage")
    extension = Path(__file__).parents[3] / "skyvern/browser_extension/extension"
    script = """
const { authorizeDomFill, fillDomInput, revokeDomFills } = await import(DOM_URI);
const { ProtocolError, ERROR_CODES } = await import(PROTOCOL_URI);
const calls = [];
let current = true;
let url = 'https://example.test/form';
let afterProbe = () => {};
let beforeAuthorize = () => {};
let beforeCommit = () => {};
let beforeOperation = async () => {};
let fillResponse = { ok: true, textLength: 3 };
const lease = {
  assertCurrent() {
    if (!current) throw new ProtocolError(ERROR_CODES.TAB_NOT_SCOPED, 'scope revoked');
  },
  remainingMs() { return 4000; },
  cancel() { current = false; },
};
const tabScope = {
  async runTabOperation(tabId, operation, generation, cancelOnEvent, onLeaseCreated) {
    if (tabId !== 7) throw new Error('wrong tab');
    onLeaseCreated?.(lease);
    await beforeOperation();
    return operation(lease);
  },
  async assertControllableLocked(tabId, token) {
    if (tabId !== 7 || token !== lease) throw new Error('scope lease missing');
    lease.assertCurrent();
    return { id: 7, url };
  },
};
globalThis.chrome = {
  runtime: { id: 'this-extension' },
  tabs: {
    async sendMessage(tabId, message, options) {
      calls.push({ tabId, message, options });
      if (message.action === 'probe') {
        afterProbe();
        return { ok: true, documentToken: 'document-a', url: 'https://example.test/form' };
      }
      beforeAuthorize();
      const authorization = { type: 'skyvern.formFillAuthorize', requestId: message.requestId, documentToken: message.documentToken, phase: 'inspect' };
      const sender = { id: chrome.runtime.id, tab: { id: tabId }, frameId: 0, documentId: 'chrome-document', url };
      const wrongTab = await authorizeDomFill(tabScope, authorization, { ...sender, tab: { id: 99 } });
      assert(wrongTab.authorized === false, 'authorization granted to another tab');
      const earlyCommit = await authorizeDomFill(tabScope, { ...authorization, phase: 'commit' }, sender);
      assert(earlyCommit.authorized === false, 'commit skipped inspection authorization');
      const grant = await authorizeDomFill(tabScope, authorization, sender);
      if (!grant.authorized) return { ok: false, error: 'authorization_lost' };
      const replay = await authorizeDomFill(tabScope, authorization, sender);
      assert(replay.authorized === false, 'authorization can be replayed');
      beforeCommit();
      const commit = { ...authorization, phase: 'commit' };
      const committed = await authorizeDomFill(tabScope, commit, sender);
      if (!committed.authorized) return { ok: false, error: 'authorization_lost' };
      const commitReplay = await authorizeDomFill(tabScope, commit, sender);
      assert(commitReplay.authorized === false, 'commit authorization can be replayed');
      return fillResponse;
    },
  },
  get userScripts() { throw new Error('User Scripts must never be accessed'); },
  get scripting() { throw new Error('arbitrary script injection must never be accessed'); },
};
const args = { tabId: 7, selector: '#email', text: 'abc', deadline: Date.now() + 4000 };
function assert(condition, message) { if (!condition) throw new Error(message); }
async function failure(input = args) {
  const error = await fillDomInput(tabScope, input).then(() => null, error => error);
  assert(error instanceof ProtocolError, 'expected structured refusal');
  return error;
}

const response = await fillDomInput(tabScope, args);
assert(response.textLength === 3, 'successful fill response');
assert(calls.length === 2, 'one probe and one fill');
assert(calls.every(call => call.tabId === 7 && call.options.frameId === 0), 'top frame of leased tab only');
const command = calls[1].message;
assert(command.documentToken === 'document-a' && command.url === url, 'document binding lost');
assert(command.selector === '#email' && command.text === 'abc', 'plain input data lost');
assert(command.expiresAt > Date.now() && command.expiresAt <= Date.now() + 1000, 'late delivery not bounded');
assert(!('expression' in command), 'no evaluation expression');

for (const invalid of [
  { ...args, expression: 'evil()' },
  { ...args, text: {} },
  { ...args, selector: '' },
  { ...args, selector: 'x'.repeat(4097) },
  { ...args, text: 'x'.repeat(65537) },
]) {
  calls.length = 0;
  await failure(invalid);
  assert(calls.length === 0, 'invalid fields reached content script');
}

calls.length = 0;
url = 'chrome-extension://another-extension/page.html';
await failure();
assert(calls.length === 0, 'restricted URL reached content script');
url = 'https://example.test/form';

calls.length = 0;
afterProbe = () => { current = false; };
await failure();
assert(calls.length === 1, 'scope revocation did not prevent fill');
current = true;

calls.length = 0;
afterProbe = () => { url = 'https://example.test/other'; };
await failure();
assert(calls.length === 1, 'navigation after probe did not prevent fill');
url = 'https://example.test/form';
afterProbe = () => {};

calls.length = 0;
beforeAuthorize = () => { current = false; };
await failure();
assert(calls.length === 2, 'expected revocation after fill delivery');
current = true;
beforeAuthorize = () => {};

calls.length = 0;
beforeCommit = () => revokeDomFills(7);
await failure();
assert(calls.length === 2, 'expected revocation after inspection grant');
current = true;
beforeCommit = () => {};

calls.length = 0;
beforeOperation = () => new Promise(resolve => setTimeout(resolve, 20));
await failure({ ...args, deadline: Date.now() + 10 });
assert(calls.length === 0, 'expired caller deadline was replaced after queueing');
beforeOperation = async () => {};
current = true;

calls.length = 0;
beforeOperation = async () => revokeDomFills(7);
await failure();
assert(calls.length === 0, 'debugger revocation did not cancel queued fill');
current = true;
beforeOperation = async () => {};

for (const code of ['document_changed', 'password_field', 'ambiguous_target', 'invalid_request']) {
  calls.length = 0;
  fillResponse = { ok: false, error: code };
  await failure();
  assert(calls.length === 2, 'failed fill was retried or bypassed');
}
console.log('PASS fixed form-fill scope, schema, document and no-fallback contract');
""".replace("DOM_URI", json.dumps((extension / "dom_router.js").as_uri())).replace(
        "PROTOCOL_URI", json.dumps((extension / "protocol.js").as_uri())
    )
    result = subprocess.run([node, "--input-type=module", "--eval", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_content_handler_rejects_untrusted_and_stale_messages_before_reading_dom() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for extension message contract coverage")
    source = Path(__file__).parents[3] / "skyvern/browser_extension/extension/form_controls.js"
    script = """
import fs from 'node:fs';
import vm from 'node:vm';
let listener;
let domReads = 0;
const window = {};
window.top = window;
const context = {
  window,
  location: { href: 'https://example.test/form', protocol: 'https:' },
  crypto: { getRandomValues(bytes) { bytes.fill(42); return bytes; } },
  Uint8Array,
  chrome: { runtime: {
    id: 'this-extension', onMessage: { addListener(value) { listener = value; } },
    async sendMessage() { return { ok: true, result: { authorized: false } }; },
  } },
  document: { querySelectorAll() { domReads++; throw new Error('must not read DOM'); } },
  eval() { throw new Error('evaluation prohibited'); },
  Function() { throw new Error('compilation prohibited'); },
};
vm.runInNewContext(fs.readFileSync(SOURCE_PATH, 'utf8'), context);
function assert(condition, message) { if (!condition) throw new Error(message); }
async function send(message, sender = { id: 'this-extension' }) {
  return new Promise(resolve => {
    const handled = listener(message, sender, resolve);
    if (handled !== true) resolve(undefined);
  });
}
const probe = { type: 'skyvern.formFill', action: 'probe' };
assert(await send(probe, { id: 'other-extension' }) === undefined, 'foreign sender accepted');
assert(await send(probe, { id: 'this-extension', tab: { id: 1 } }) === undefined, 'content sender accepted');
assert((await send({ ...probe, expression: 'evil()' })).error === 'invalid_request', 'probe accepted extra code');
const ready = await send(probe);
assert(ready.ok === true && ready.documentToken, 'trusted probe refused');
const fill = {
  type: 'skyvern.formFill', action: 'fill', requestId: 'request-a', documentToken: ready.documentToken,
  url: context.location.href, expiresAt: Date.now() + 500, selector: '#email', text: 'example',
};
for (const invalid of [
  { ...fill, documentToken: 'old-document' },
  { ...fill, url: 'https://example.test/other' },
  { ...fill, expiresAt: 0 },
  { ...fill, expiresAt: Date.now() + 10000 },
]) assert((await send(invalid)).error === 'document_changed', 'stale fill accepted');
for (const invalid of [
  { ...fill, expression: 'evil()' },
  { ...fill, action: 'evaluate' },
  { ...fill, text: {} },
]) assert((await send(invalid)).error === 'invalid_request', 'unstructured input accepted');
assert((await send(fill)).error === 'authorization_lost', 'unauthorized fill accepted');
context.chrome.runtime.sendMessage = async () => {
  context.Date = { now: () => Date.now() + 2000 };
  return { ok: true, result: { authorized: true } };
};
assert((await send(fill)).error === 'document_changed', 'fill applied after authorization deadline');
assert(domReads === 0, 'rejected request accessed DOM');
console.log('PASS fixed form sender, document, expiry and schema boundaries');
""".replace("SOURCE_PATH", json.dumps(str(source)))
    result = subprocess.run([node, "--input-type=module", "--eval", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
