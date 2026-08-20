export const LEGACY_PROTOCOL_VERSION = 1;
export const PROTOCOL_VERSION = 2;
export const DEFAULT_BRIDGE_PORT = 19777;
export const BRIDGE_ALARM_NAME = "skyvern-bridge-reconnect";

export const MESSAGE_TYPES = Object.freeze({
  AUTH_CHALLENGE: "auth.challenge",
  AUTH_PROOF: "auth.proof",
  AUTH_OK: "auth.ok",
  REQUEST: "request",
  RESPONSE: "response",
  EVENT: "event",
  PING: "ping",
  PONG: "pong",
  EXTENSION_RESET: "extension.reset",
  EXTENSION_RESET_ACK: "extension.reset_ack",
});

export const OPS = Object.freeze({
  DEBUGGER_ATTACH: "debugger.attach",
  DEBUGGER_DETACH: "debugger.detach",
  DEBUGGER_SEND: "debugger.send",
  SHARE_TAB: "shareTab",
  UNSHARE_TAB: "unshareTab",
  TABS_CREATE: "tabs.create",
  TABS_REMOVE: "tabs.remove",
  TABS_ACTIVATE: "tabs.activate",
  TABS_LIST: "tabs.list",
});

export const EVENTS = Object.freeze({
  EXTENSION_HELLO: "extension.hello",
  DEBUGGER_EVENT: "debugger.event",
  DEBUGGER_DETACHED: "debugger.detached",
  SCOPE_TAB_ADDED: "scope.tabAdded",
  SCOPE_TAB_REMOVED: "scope.tabRemoved",
  TABS_CREATED: "tabs.created",
});

export const ERROR_CODES = Object.freeze({
  AUTH_FAILED: "AUTH_FAILED",
  OP_NOT_ALLOWED: "OP_NOT_ALLOWED",
  TAB_NOT_FOUND: "TAB_NOT_FOUND",
  TAB_NOT_SCOPED: "TAB_NOT_SCOPED",
  RESTRICTED_URL: "RESTRICTED_URL",
  DEBUGGER_DETACHED: "DEBUGGER_DETACHED",
  CDP_METHOD_NOT_ALLOWED: "CDP_METHOD_NOT_ALLOWED",
  CDP_ERROR: "CDP_ERROR",
  INTERNAL: "INTERNAL",
});

export const ALLOWED_CDP_PREFIXES = new Set([
  "Accessibility",
  "Animation",
  "CSS",
  "Console",
  "DOM",
  "DOMDebugger",
  "DOMSnapshot",
  "DOMStorage",
  "Debugger",
  "Emulation",
  "Fetch",
  "IO",
  "Input",
  "Inspector",
  "Log",
  "Network",
  "Overlay",
  "Page",
  "Performance",
  "Profiler",
  "Runtime",
  "Security",
  "Storage",
  "Target",
]);

export const DENIED_CDP_METHODS = new Set([
  "Network.getAllCookies",
  "Network.clearBrowserCookies",
  "Network.clearBrowserCache",
  "Storage.getCookies",
  "Storage.setCookies",
  "Storage.clearCookies",
]);

export class ProtocolError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "ProtocolError";
    this.code = code;
  }
}

export function isRestrictedUrl(url) {
  if (typeof url !== "string" || url.length === 0) {
    return false;
  }

  const normalized = url.trim().toLowerCase();
  if (normalized === "about:blank") {
    return false;
  }
  if (
    normalized.startsWith("chrome://") ||
    normalized.startsWith("chrome-untrusted://") ||
    normalized.startsWith("chrome-extension://") ||
    normalized.startsWith("devtools://") ||
    normalized.startsWith("edge://") ||
    normalized.startsWith("about:") ||
    normalized.startsWith("file://")
  ) {
    return true;
  }

  try {
    const hostname = new URL(url).hostname.toLowerCase().replace(/\.$/, "");
    return hostname === "chromewebstore.google.com";
  } catch {
    return false;
  }
}

export function requireArgs(args) {
  if (args === null || typeof args !== "object" || Array.isArray(args)) {
    throw new ProtocolError(
      ERROR_CODES.INTERNAL,
      "Request arguments must be an object.",
    );
  }
  return args;
}

export function requireTabId(value) {
  if (!Number.isInteger(value) || value < 0) {
    throw new ProtocolError(
      ERROR_CODES.TAB_NOT_FOUND,
      "The requested tab was not found.",
    );
  }
  return value;
}

export function protocolErrorEnvelope(error) {
  if (
    error instanceof ProtocolError &&
    Object.values(ERROR_CODES).includes(error.code)
  ) {
    return { code: error.code, message: error.message };
  }
  return {
    code: ERROR_CODES.INTERNAL,
    message: "The extension operation failed.",
  };
}
