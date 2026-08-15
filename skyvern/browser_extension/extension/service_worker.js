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

let debuggerRouter;
let pendingPairingOffer = null;
let pairingOfferQueue = Promise.resolve();
let resetQueue = Promise.resolve();
let lastResetEpoch = null;
let lastResetGeneration = -1;
let lastResetOk = null;

const bridge = new BridgeConnection({
  onRequest: (op, args) => dispatchRequest(op, args),
  onAuthenticated: () => sendHello(),
  onReset: (epoch, generation) => enqueueReset(epoch, generation),
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
    scopedTabs: await tabScope.helloTabs(),
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
        const { failedTabCount } = await debuggerRouter.reset();
        const ok = failedTabCount === 0;
        if (ok) {
          await tabScope.reset();
        }
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

function parsePairingOffer(message, sender) {
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
    message.token.length === 0
  ) {
    return null;
  }

  let senderUrl;
  try {
    senderUrl = new URL(sender.url);
  } catch {
    return null;
  }
  if (
    senderUrl.protocol !== "http:" ||
    (senderUrl.hostname !== "127.0.0.1" &&
      senderUrl.hostname !== "localhost") ||
    senderUrl.port === "" ||
    Number(senderUrl.port) !== message.port
  ) {
    return null;
  }
  return { port: message.port, token: message.token };
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
    offer.token.length === 0
  ) {
    return null;
  }
  pendingPairingOffer = { port: offer.port, token: offer.token };
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
  await clearPendingPairingOffer();
  return bridge.connect();
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
