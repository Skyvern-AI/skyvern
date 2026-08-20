const PENDING_PAIRING_STORAGE_KEY = "pendingPairingOffer";

const elements = {
  question: document.querySelector("#pairing-question"),
  port: document.querySelector("#pairing-port"),
  fingerprint: document.querySelector("#pairing-fingerprint"),
  approveButton: document.querySelector("#approve-button"),
  approveLabel: document.querySelector("#approve-label"),
  cancelButton: document.querySelector("#cancel-button"),
  actions: document.querySelector(".actions"),
  result: document.querySelector("#pairing-result"),
  resultTitle: document.querySelector("#result-title"),
  resultMessage: document.querySelector("#result-message"),
  recovery: document.querySelector("#recovery"),
};

let renderGeneration = 0;
let operationInProgress = false;

async function sendMessage(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) {
    throw new Error(
      response?.error?.message || "The extension request failed.",
    );
  }
  return response.result;
}

function isPendingOffer(offer) {
  return (
    offer !== null &&
    typeof offer === "object" &&
    !Array.isArray(offer) &&
    Number.isInteger(offer.port) &&
    offer.port >= 1 &&
    offer.port <= 65_535 &&
    typeof offer.token === "string" &&
    offer.token.length > 0
  );
}

async function fingerprint(token) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(token),
  );
  return Array.from(new Uint8Array(digest), (byte) =>
    byte.toString(16).padStart(2, "0"),
  )
    .join("")
    .slice(0, 8);
}

function showResult({
  state,
  title,
  message,
  recoverable = false,
  terminal = false,
}) {
  document.body.dataset.state = state;
  elements.actions.dataset.terminal = String(terminal);
  elements.result.hidden = false;
  elements.resultTitle.textContent = title;
  elements.resultMessage.textContent = message;
  elements.recovery.hidden = !recoverable;
}

function clearResult() {
  document.body.dataset.state = "idle";
  elements.actions.dataset.terminal = "false";
  elements.result.hidden = true;
  elements.resultTitle.textContent = "";
  elements.resultMessage.textContent = "";
  elements.recovery.hidden = true;
}

async function renderPendingOffer() {
  const generation = ++renderGeneration;
  const stored = await chrome.storage.session.get(PENDING_PAIRING_STORAGE_KEY);
  const offer = stored[PENDING_PAIRING_STORAGE_KEY];
  if (!isPendingOffer(offer)) {
    elements.question.textContent =
      "This one-time pairing offer is no longer available.";
    elements.port.textContent = "Unavailable";
    elements.fingerprint.textContent = "--------";
    elements.approveButton.disabled = true;
    elements.cancelButton.disabled = true;
    showResult({
      state: "error",
      title: "Pairing request expired",
      message: "The secure pairing request expired before approval.",
      recoverable: true,
      terminal: true,
    });
    return;
  }
  const tokenFingerprint = await fingerprint(offer.token);
  if (generation !== renderGeneration) {
    return;
  }
  elements.question.textContent =
    "Skyvern on this computer wants to drive Chrome through this extension.";
  elements.port.textContent = `127.0.0.1:${offer.port}`;
  elements.fingerprint.textContent = tokenFingerprint;
  elements.approveButton.disabled = false;
  elements.cancelButton.disabled = false;
  clearResult();
}

function disableActions() {
  elements.approveButton.disabled = true;
  elements.cancelButton.disabled = true;
}

function finishBusyState() {
  elements.approveButton.removeAttribute("aria-busy");
  elements.approveLabel.textContent = "Approve pairing";
}

elements.approveButton.addEventListener("click", () => {
  operationInProgress = true;
  disableActions();
  document.body.dataset.state = "approving";
  elements.approveButton.setAttribute("aria-busy", "true");
  elements.approveLabel.textContent = "Connecting…";
  elements.result.hidden = true;
  void sendMessage({ type: "applyPendingPairing" })
    .then(() => {
      finishBusyState();
      showResult({
        state: "success",
        title: "Connected",
        message:
          "Skyvern Agent is now paired with your local MCP server. You can close this tab.",
        terminal: true,
      });
    })
    .catch((error) => {
      operationInProgress = false;
      finishBusyState();
      showResult({
        state: "error",
        title: "Connection failed",
        message: error.message,
      });
      elements.approveButton.disabled = false;
      elements.cancelButton.disabled = false;
    });
});

elements.cancelButton.addEventListener("click", () => {
  operationInProgress = true;
  disableActions();
  elements.result.hidden = true;
  void sendMessage({ type: "cancelPendingPairing" })
    .then(() => {
      showResult({
        state: "cancelled",
        title: "Pairing cancelled",
        message: "No connection was made. You can close this tab.",
        terminal: true,
      });
    })
    .catch((error) => {
      operationInProgress = false;
      showResult({
        state: "error",
        title: "Couldn’t cancel pairing",
        message: error.message,
      });
      elements.cancelButton.disabled = false;
    });
});

chrome.storage.onChanged.addListener((changes, areaName) => {
  if (
    !operationInProgress &&
    areaName === "session" &&
    PENDING_PAIRING_STORAGE_KEY in changes
  ) {
    void renderPendingOffer();
  }
});

await renderPendingOffer();
