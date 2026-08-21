import { BridgeConnection } from "./bridge_connection.js";
import { DebuggerRouter } from "./debugger_router.js";
import {
  ERROR_CODES,
  EVENTS,
  OPS,
  PROTOCOL_VERSION,
  ProtocolError,
  requireArgs,
} from "./protocol.js";
import { TabScope } from "./tab_scope.js";

const PENDING_PAIRING_STORAGE_KEY = "pendingPairingOffer";
const PAIRING_CONFIRM_PATH = "pairing_confirm.html";
const PAIRING_APPROVAL_TIMEOUT_MS = 15_000;

let debuggerRouter;
let pendingPairingOffer = null;
let pairingOfferQueue = Promise.resolve();
const pendingPairingApprovals = new Map();
let resetQueue = Promise.resolve();
let lastResetEpoch = null;
let lastResetGeneration = -1;
let lastResetOk = null;

const bridge = new BridgeConnection({
  onRequest: (op, args) => dispatchRequest(op, args),
  onAuthenticated: async () => {
    await sendHello();
    resendPendingPairingApprovals();
  },
  onReset: (epoch, generation) => enqueueReset(epoch, generation),
  onEvent: (event, params) => handleBrokerEvent(event, params),
  onStateChange: () => updateActionState(),
});

const tabScope = new TabScope({
  sendEvent: (event, params) => bridge.sendEvent(event, params),
});

debuggerRouter = new DebuggerRouter({
  tabScope,
  sendEvent: (event, params) => bridge.sendEvent(event, params),
  onAttachedChange: () => updateActionState(),
});
tabScope.setDebuggerRouter(debuggerRouter);

const handlers = new Map([
  [OPS.DEBUGGER_ATTACH, (args) => debuggerRouter.attach(args)],
  [OPS.DEBUGGER_DETACH, (args) => debuggerRouter.detach(args)],
  [OPS.DEBUGGER_SEND, (args) => debuggerRouter.send(args)],
  [OPS.TABS_CREATE, (args) => tabScope.create(args)],
  [OPS.TABS_REMOVE, (args) => tabScope.remove(args)],
  [OPS.TABS_ACTIVATE, (args) => tabScope.activate(args)],
  [
    OPS.TABS_LIST,
    (args) => {
      requireArgs(args);
      return tabScope.list();
    },
  ],
]);

async function dispatchRequest(op, args) {
  const handler = handlers.get(op);
  if (handler === undefined) {
    throw new ProtocolError(
      ERROR_CODES.OP_NOT_ALLOWED,
      "The requested operation is not allowed.",
    );
  }
  return handler(args);
}

async function sendHello() {
  bridge.sendEvent(EVENTS.EXTENSION_HELLO, {
    protocolVersion: PROTOCOL_VERSION,
    extensionVersion: chrome.runtime.getManifest().version,
    scopeEventOrigins: true,
    scopedTabs: await tabScope.helloTabs(),
  });
}
function handleBrokerEvent(event, params) {
  if (
    event !== EVENTS.PAIRING_APPROVED_ACK ||
    typeof params?.approvalNonce !== "string" ||
    params.approved !== true
  ) {
    return;
  }
  const pending = pendingPairingApprovals.get(params.approvalNonce);
  if (pending === undefined) {
    return;
  }
  pendingPairingApprovals.delete(params.approvalNonce);
  clearTimeout(pending.timeout);
  pending.resolve();
}

function resendPendingPairingApprovals() {
  for (const approvalNonce of pendingPairingApprovals.keys()) {
    bridge.sendEvent(EVENTS.PAIRING_APPROVED, { approvalNonce });
  }
}

async function waitForBridgeConnection() {
  const deadline = Date.now() + PAIRING_APPROVAL_TIMEOUT_MS;
  while (!bridge.getStatus().connected) {
    if (Date.now() >= deadline) {
      throw new ProtocolError(
        ERROR_CODES.INTERNAL,
        "The local broker did not connect before approval expired.",
      );
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
}

function approveBrokerClient(approvalNonce) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => {
      pendingPairingApprovals.delete(approvalNonce);
      reject(
        new ProtocolError(
          ERROR_CODES.INTERNAL,
          "The local broker did not confirm this agent approval.",
        ),
      );
    }, PAIRING_APPROVAL_TIMEOUT_MS);
    pendingPairingApprovals.set(approvalNonce, { resolve, timeout });
    if (!bridge.sendEvent(EVENTS.PAIRING_APPROVED, { approvalNonce })) {
      pendingPairingApprovals.delete(approvalNonce);
      clearTimeout(timeout);
      reject(
        new ProtocolError(
          ERROR_CODES.INTERNAL,
          "The local broker disconnected before approval.",
        ),
      );
    }
  });
}

function enqueueReset(epoch, generation) {
  const reset = resetQueue
    .catch(() => undefined)
    .then(async () => {
      if (
        epoch === lastResetEpoch &&
        generation === lastResetGeneration &&
        lastResetOk === true
      ) {
        return { executed: false, ok: true, failedTabCount: 0 };
      }
      if (epoch === lastResetEpoch && generation < lastResetGeneration) {
        return null;
      }
      await tabScope.prepareForReset();
      try {
        let { failedTabCount } = await debuggerRouter.reset();
        if (failedTabCount === 0) {
          const scopeReset = await tabScope.reset();
          failedTabCount += scopeReset.failedTabCount;
        }
        const ok = failedTabCount === 0;
        lastResetEpoch = epoch;
        lastResetGeneration = generation;
        lastResetOk = ok;
        return { executed: true, ok, failedTabCount };
      } finally {
        tabScope.finishReset();
      }
    });
  resetQueue = reset;
  return reset;
}

async function updateActionState() {
  const connected = bridge.getStatus().connected;
  await chrome.action.setBadgeText({ text: connected ? "ON" : "" });
  await chrome.action.setBadgeBackgroundColor({ color: "#6D6CF6" });
}

async function popupStatus() {
  const status = bridge.getStatus();
  return {
    ...status,
    clientAttached: status.connected && debuggerRouter.hasAttachedTabs(),
    protocolVersion: PROTOCOL_VERSION,
  };
}

function isTrustedPairingSender(sender, port) {
  let senderUrl;
  try {
    senderUrl = new URL(sender.url);
  } catch {
    return false;
  }
  return (
    senderUrl.protocol === "http:" &&
    (senderUrl.hostname === "127.0.0.1" ||
      senderUrl.hostname === "localhost") &&
    senderUrl.port !== "" &&
    Number(senderUrl.port) === port
  );
}

function isPairingProbe(message, sender) {
  return (
    message !== null &&
    typeof message === "object" &&
    !Array.isArray(message) &&
    message.type === "skyvern.pairingProbe" &&
    message.v === 1 &&
    Number.isInteger(message.port) &&
    message.port >= 1 &&
    message.port <= 65_535 &&
    isTrustedPairingSender(sender, message.port)
  );
}

function parsePairingOffer(message, sender) {
  const approvalFieldsPresent =
    message?.approvalNonce !== undefined ||
    message?.requestFingerprint !== undefined;
  const validApproval =
    !approvalFieldsPresent ||
    (typeof message.approvalNonce === "string" &&
      message.approvalNonce.length > 0 &&
      typeof message.requestFingerprint === "string" &&
      message.requestFingerprint.length > 0);
  if (
    message === null ||
    typeof message !== "object" ||
    Array.isArray(message) ||
    message.type !== "skyvern.pairingOffer" ||
    message.v !== 1 ||
    !Number.isInteger(message.port) ||
    message.port < 1 ||
    message.port > 65_535 ||
    typeof message.token !== "string" ||
    message.token.length === 0 ||
    !validApproval ||
    !isTrustedPairingSender(sender, message.port)
  ) {
    return null;
  }
  return {
    port: message.port,
    token: message.token,
    approvalNonce: message.approvalNonce,
    requestFingerprint: message.requestFingerprint,
  };
}

async function focusPairingConfirmation() {
  const confirmationUrl = chrome.runtime.getURL(PAIRING_CONFIRM_PATH);
  const tabs = await chrome.tabs.query({});
  const existingTab = tabs.find((tab) => tab.url === confirmationUrl);
  if (Number.isInteger(existingTab?.id)) {
    await chrome.tabs.update(existingTab.id, { active: true });
    if (
      Number.isInteger(existingTab.windowId) &&
      typeof chrome.windows?.update === "function"
    ) {
      await chrome.windows.update(existingTab.windowId, { focused: true });
    }
    return;
  }
  await chrome.tabs.create({ active: true, url: confirmationUrl });
}

async function acceptPairingOffer(offer) {
  pendingPairingOffer = offer;
  await chrome.storage.session.set({ [PENDING_PAIRING_STORAGE_KEY]: offer });
  await focusPairingConfirmation();
}

function enqueuePairingOperation(operation) {
  const result = pairingOfferQueue
    .catch(() => undefined)
    .then(() => operation());
  pairingOfferQueue = result;
  return result;
}

async function readPendingPairingOffer() {
  if (pendingPairingOffer !== null) {
    return pendingPairingOffer;
  }
  const stored = await chrome.storage.session.get(PENDING_PAIRING_STORAGE_KEY);
  const offer = stored[PENDING_PAIRING_STORAGE_KEY];
  if (
    offer === null ||
    typeof offer !== "object" ||
    Array.isArray(offer) ||
    !Number.isInteger(offer.port) ||
    offer.port < 1 ||
    offer.port > 65_535 ||
    typeof offer.token !== "string" ||
    offer.token.length === 0 ||
    (offer.approvalNonce !== undefined &&
      (typeof offer.approvalNonce !== "string" ||
        typeof offer.requestFingerprint !== "string"))
  ) {
    return null;
  }
  pendingPairingOffer = offer;
  return pendingPairingOffer;
}

async function clearPendingPairingOffer() {
  pendingPairingOffer = null;
  await chrome.storage.session.remove(PENDING_PAIRING_STORAGE_KEY);
}

async function applyPendingPairing() {
  const offer = await readPendingPairingOffer();
  if (offer === null) {
    throw new ProtocolError(
      ERROR_CODES.INTERNAL,
      "No pending pairing offer is available.",
    );
  }
  await bridge.setConfig({
    bridgePort: offer.port,
    pairingToken: offer.token,
  });
  await bridge.connect();
  if (typeof offer.approvalNonce === "string") {
    await waitForBridgeConnection();
    await approveBrokerClient(offer.approvalNonce);
  }
  await clearPendingPairingOffer();
  return {};
}

async function handlePopupMessage(message) {
  if (
    message === null ||
    typeof message !== "object" ||
    Array.isArray(message)
  ) {
    throw new ProtocolError(
      ERROR_CODES.INTERNAL,
      "The popup request is invalid.",
    );
  }
  switch (message.type) {
    case "getStatus":
      return popupStatus();
    case "setConfig":
      return bridge.setConfig(requireArgs(message.config));
    case "applyPendingPairing":
      return enqueuePairingOperation(applyPendingPairing);
    case "cancelPendingPairing":
      return enqueuePairingOperation(clearPendingPairingOffer);
    case "connect":
      return bridge.connect();
    case "disconnect":
      return bridge.disconnect();
    case "shareTab":
      return tabScope.shareTab(message.tabId);
    case "unshareTab":
      return tabScope.unshareTab(message.tabId);
    case "listScoped":
      return tabScope.list();
    default:
      throw new ProtocolError(
        ERROR_CODES.OP_NOT_ALLOWED,
        "The popup operation is not allowed.",
      );
  }
}

chrome.runtime.onMessageExternal.addListener(
  (message, sender, sendResponse) => {
    if (isPairingProbe(message, sender)) {
      sendResponse({ ok: true, available: true });
      return false;
    }
    const offer = parsePairingOffer(message, sender);
    if (offer === null) {
      sendResponse({ ok: false, error: "invalid_offer" });
      return false;
    }

    const acceptance = enqueuePairingOperation(() => acceptPairingOffer(offer));
    void acceptance
      .then(() => sendResponse({ ok: true, pending: true }))
      .catch(() => sendResponse({ ok: false, error: "pairing_failed" }));
    return true;
  },
);

// Chrome fails the whole service worker if module evaluation uses top-level await
// (event dispatch never starts), so initialization must run as a tracked promise.
const initialized = (async () => {
  await tabScope.initialize();
  await bridge.initialize();
  await updateActionState();
})();
initialized.catch((error) =>
  console.error("Skyvern bridge initialization failed", error),
);

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  void initialized
    .catch(() => undefined)
    .then(() => handlePopupMessage(message))
    .then((result) => sendResponse({ ok: true, result }))
    .catch((error) => {
      const code =
        error instanceof ProtocolError ? error.code : ERROR_CODES.INTERNAL;
      const messageText =
        error instanceof ProtocolError
          ? error.message
          : "The extension operation failed.";
      sendResponse({ ok: false, error: { code, message: messageText } });
    });
  return true;
});
