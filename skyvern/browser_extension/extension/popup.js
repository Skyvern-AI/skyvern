import { DEFAULT_BRIDGE_PORT, isRestrictedUrl } from "./protocol.js";

const elements = {
  statusDot: document.querySelector("#status-dot"),
  statusLabel: document.querySelector("#status-label"),
  advancedSettings: document.querySelector("#advanced-settings"),
  port: document.querySelector("#bridge-port"),
  token: document.querySelector("#pairing-token"),
  tokenVisibility: document.querySelector("#pairing-token-visibility"),
  eyeIcon: document.querySelector(".eye-icon"),
  eyeOffIcon: document.querySelector(".eye-off-icon"),
  connectionButton: document.querySelector("#connection-button"),
  connectionError: document.querySelector("#connection-error"),
  shareButton: document.querySelector("#share-button"),
  shareReason: document.querySelector("#share-reason"),
  sharedTabs: document.querySelector("#shared-tabs"),
  emptyTabs: document.querySelector("#empty-tabs"),
};

let status = { connected: false, clientAttached: false, enabled: false };
let activeTab = null;
let scopedTabs = [];
let configTimer = null;
let configLoaded = false;

async function sendMessage(message) {
  const response = await chrome.runtime.sendMessage(message);
  if (!response?.ok) {
    throw new Error(
      response?.error?.message || "The extension request failed.",
    );
  }
  return response.result;
}

function loadConfig(config) {
  elements.port.value = String(config.bridgePort);
  elements.advancedSettings.open =
    Number(config.bridgePort) !== DEFAULT_BRIDGE_PORT;
  elements.token.value =
    typeof config.pairingToken === "string" ? config.pairingToken : "";
  configLoaded = true;
}

async function saveConfig() {
  window.clearTimeout(configTimer);
  configTimer = null;
  await sendMessage({
    type: "setConfig",
    config: {
      bridgePort: Number(elements.port.value),
      pairingToken: elements.token.value,
    },
  });
}

function scheduleConfigSave() {
  window.clearTimeout(configTimer);
  configTimer = window.setTimeout(() => {
    void saveConfig().catch(showConnectionError);
  }, 250);
}

function showConnectionError(error) {
  elements.connectionError.textContent =
    error instanceof Error ? error.message : "The extension request failed.";
}

function setTokenVisibility(visible) {
  elements.token.type = visible ? "text" : "password";
  elements.tokenVisibility.setAttribute("aria-pressed", String(visible));
  elements.tokenVisibility.setAttribute(
    "aria-label",
    visible ? "Hide pairing token" : "Show pairing token",
  );
  elements.eyeIcon.hidden = visible;
  elements.eyeOffIcon.hidden = !visible;
}

function renderStatus() {
  const label = status.clientAttached
    ? "Client attached"
    : status.connected
      ? "Connected"
      : "Disconnected";
  elements.statusLabel.textContent = label;
  elements.statusDot.className = `status-dot ${status.connected ? "connected" : ""}`;
  elements.connectionButton.textContent = status.enabled
    ? "Disconnect"
    : "Connect";
}

function renderCurrentTab() {
  const restricted =
    activeTab === null ||
    isRestrictedUrl(activeTab.pendingUrl || activeTab.url || "");
  const alreadyShared =
    activeTab !== null && scopedTabs.some((tab) => tab.tabId === activeTab.id);
  elements.shareButton.disabled = restricted || alreadyShared;
  if (restricted) {
    elements.shareReason.textContent =
      "Chrome does not allow adding this page.";
  } else if (alreadyShared) {
    elements.shareReason.textContent =
      "This tab is already in Skyvern Controlled.";
  } else {
    elements.shareReason.textContent = "";
  }
}

function renderScopedTabs() {
  elements.sharedTabs.replaceChildren();
  elements.emptyTabs.hidden = scopedTabs.length > 0;
  for (const tab of scopedTabs) {
    const item = document.createElement("li");
    const text = document.createElement("span");
    const button = document.createElement("button");
    text.className = "tab-title";
    text.textContent = tab.title || tab.url || `Tab ${tab.tabId}`;
    text.title = tab.url || "";
    button.type = "button";
    button.textContent = "Remove from Skyvern Controlled";
    button.addEventListener("click", () => {
      button.disabled = true;
      void sendMessage({ type: "unshareTab", tabId: tab.tabId })
        .then(refresh)
        .catch((error) => {
          button.disabled = false;
          showConnectionError(error);
        });
    });
    item.append(text, button);
    elements.sharedTabs.append(item);
  }
}

async function refresh() {
  const [nextStatus, scopedResult, activeTabs] = await Promise.all([
    sendMessage({ type: "getStatus" }),
    sendMessage({ type: "listScoped" }),
    chrome.tabs.query({ active: true, currentWindow: true }),
  ]);
  status = nextStatus;
  if (!configLoaded) {
    loadConfig(nextStatus);
  }
  scopedTabs = scopedResult.tabs;
  activeTab = activeTabs[0] ?? null;
  elements.connectionError.textContent = status.lastError || "";
  renderStatus();
  renderCurrentTab();
  renderScopedTabs();
}

elements.port.addEventListener("input", scheduleConfigSave);
elements.token.addEventListener("input", scheduleConfigSave);
elements.tokenVisibility.addEventListener("click", () => {
  setTokenVisibility(elements.token.type === "password");
});
elements.connectionButton.addEventListener("click", () => {
  elements.connectionButton.disabled = true;
  elements.connectionError.textContent = "";
  const action = status.enabled
    ? sendMessage({ type: "disconnect" })
    : saveConfig().then(() => sendMessage({ type: "connect" }));
  void action
    .then(refresh)
    .catch(showConnectionError)
    .finally(() => {
      elements.connectionButton.disabled = false;
    });
});
elements.shareButton.addEventListener("click", () => {
  if (activeTab === null || !Number.isInteger(activeTab.id)) {
    return;
  }
  elements.shareButton.disabled = true;
  void sendMessage({ type: "shareTab", tabId: activeTab.id })
    .then(refresh)
    .catch(showConnectionError);
});

await refresh();
window.setInterval(() => {
  void refresh().catch(showConnectionError);
}, 2_000);
