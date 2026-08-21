import {
  BRIDGE_ALARM_NAME,
  DEFAULT_BRIDGE_PORT,
  ERROR_CODES,
  LEGACY_PROTOCOL_VERSION,
  MESSAGE_TYPES,
  PROTOCOL_VERSION,
  ProtocolError,
  protocolErrorEnvelope,
} from "./protocol.js";

const PING_INTERVAL_MS = 20_000;
const SILENCE_TIMEOUT_MS = 45_000;
const CONNECT_TIMEOUT_MS = 10_000;
const RECONNECT_MAX_MS = 10_000;
const AUTH_EXT_CONTEXT = "skyvern-ext-v1|";
const AUTH_SERVER_CONTEXT = "skyvern-srv-v1|";

function bytesToBase64Url(bytes) {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/, "");
}

function base64UrlToBytes(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new ProtocolError(
      ERROR_CODES.AUTH_FAILED,
      "Bridge authentication failed.",
    );
  }
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  try {
    const binary = atob(
      value.replaceAll("-", "+").replaceAll("_", "/") + padding,
    );
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    throw new ProtocolError(
      ERROR_CODES.AUTH_FAILED,
      "Bridge authentication failed.",
    );
  }
}

function constantTimeEqual(left, right) {
  const length = Math.max(left.length, right.length);
  let difference = left.length ^ right.length;
  for (let index = 0; index < length; index += 1) {
    difference |= (left[index] ?? 0) ^ (right[index] ?? 0);
  }
  return difference === 0;
}

async function importHmacKey(token) {
  return crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(token),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
}

async function signHmac(key, message) {
  const signature = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(message),
  );
  return new Uint8Array(signature);
}

export class BridgeConnection {
  constructor({
    onRequest,
    onAuthenticated,
    onReset,
    onEvent = async () => undefined,
    onStateChange,
  }) {
    this.onRequest = onRequest;
    this.onAuthenticated = onAuthenticated;
    this.onReset = onReset;
    this.onEvent = onEvent;
    this.onStateChange = onStateChange;
    this.socket = null;
    this.authenticated = false;
    this.enabled = false;
    this.port = DEFAULT_BRIDGE_PORT;
    this.token = "";
    this.lastError = "";
    this.lastMessageAt = 0;
    this.reconnectDelayMs = 1_000;
    this.reconnectTimer = null;
    this.connectTimer = null;
    this.pingTimer = null;
    this.silenceTimer = null;
    this.connectionGeneration = 0;
    this.clientNonce = null;
    this.serverNonce = null;
    this.hmacKey = null;
    this.messageQueue = Promise.resolve();

    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === BRIDGE_ALARM_NAME) {
        void this.kick();
      }
    });
  }

  async initialize() {
    const stored = await chrome.storage.local.get({
      bridgePort: DEFAULT_BRIDGE_PORT,
      pairingToken: "",
      enabled: false,
    });
    this.port = this.normalizePort(stored.bridgePort);
    this.token =
      typeof stored.pairingToken === "string" ? stored.pairingToken : "";
    this.enabled = stored.enabled === true;
    await chrome.alarms.create(BRIDGE_ALARM_NAME, { periodInMinutes: 0.5 });
    this.notifyState();
    if (this.enabled && this.token) {
      await this.kick();
    }
  }

  getStatus() {
    return {
      connected: this.authenticated,
      enabled: this.enabled,
      bridgePort: this.port,
      pairingToken: this.token,
      lastError: this.lastError,
    };
  }

  async setConfig({ bridgePort, pairingToken }) {
    const port = this.normalizePort(bridgePort);
    if (typeof pairingToken !== "string") {
      throw new ProtocolError(
        ERROR_CODES.AUTH_FAILED,
        "A pairing token is required.",
      );
    }
    const token = pairingToken.trim();
    const changed = port !== this.port || token !== this.token;
    this.port = port;
    this.token = token;
    await chrome.storage.local.set({ bridgePort: port, pairingToken: token });
    if (changed && this.enabled) {
      this.closeSocket();
      await this.kick();
    }
    return {};
  }

  async connect() {
    if (!this.token) {
      throw new ProtocolError(
        ERROR_CODES.AUTH_FAILED,
        "A pairing token is required.",
      );
    }
    this.enabled = true;
    this.lastError = "";
    this.reconnectDelayMs = 1_000;
    await chrome.storage.local.set({ enabled: true });
    await this.kick();
    return {};
  }

  async disconnect() {
    this.enabled = false;
    this.lastError = "";
    await chrome.storage.local.set({ enabled: false });
    this.clearReconnectTimer();
    this.closeSocket();
    this.notifyState();
    return {};
  }

  async kick() {
    if (!this.enabled || !this.token || this.socket !== null) {
      return;
    }

    const generation = ++this.connectionGeneration;
    let socket;
    try {
      socket = new WebSocket(`ws://127.0.0.1:${this.port}/extension/v1`);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;
    this.authenticated = false;
    this.clientNonce = null;
    this.serverNonce = null;
    this.hmacKey = null;
    this.notifyState();
    this.connectTimer = setTimeout(() => {
      if (generation !== this.connectionGeneration || this.socket !== socket) {
        return;
      }
      this.connectTimer = null;
      if (socket.readyState === WebSocket.CONNECTING) {
        this.failConnection("Could not reach the local Skyvern bridge.");
      }
    }, CONNECT_TIMEOUT_MS);

    socket.addEventListener("open", () => {
      if (generation === this.connectionGeneration) {
        this.clearConnectTimer();
        this.lastMessageAt = Date.now();
        this.startHealthTimers();
      }
    });
    socket.addEventListener("message", (event) => {
      if (generation === this.connectionGeneration) {
        const handling = this.messageQueue
          .catch(() => undefined)
          .then(() => {
            if (
              generation !== this.connectionGeneration ||
              this.socket !== socket
            ) {
              return undefined;
            }
            return this.handleMessage(event.data);
          });
        this.messageQueue = handling;
        void handling;
      }
    });
    socket.addEventListener("error", () => {
      if (generation === this.connectionGeneration) {
        this.failConnection("Could not reach the local Skyvern bridge.");
      }
    });
    socket.addEventListener("close", () => {
      if (generation !== this.connectionGeneration) {
        return;
      }
      this.socket = null;
      this.authenticated = false;
      this.clearConnectTimer();
      this.stopHealthTimers();
      this.notifyState();
      this.scheduleReconnect();
    });
  }

  async handleMessage(rawMessage) {
    this.lastMessageAt = Date.now();
    let message;
    try {
      message = JSON.parse(rawMessage);
    } catch {
      this.failConnection("The local bridge sent an invalid message.");
      return;
    }

    const legacyChallenge =
      !this.authenticated &&
      message?.v === LEGACY_PROTOCOL_VERSION &&
      message?.type === MESSAGE_TYPES.AUTH_CHALLENGE;
    if (
      (message?.v !== PROTOCOL_VERSION && !legacyChallenge) ||
      typeof message.type !== "string"
    ) {
      this.failConnection("The local bridge protocol is incompatible.");
      return;
    }

    try {
      if (!this.authenticated) {
        await this.handleAuthMessage(message);
        return;
      }
      if (message.type === MESSAGE_TYPES.PING) {
        this.sendRaw({ v: PROTOCOL_VERSION, type: MESSAGE_TYPES.PONG });
        return;
      }
      if (message.type === MESSAGE_TYPES.PONG) {
        return;
      }
      if (message.type === MESSAGE_TYPES.REQUEST) {
        await this.handleRequest(message);
        return;
      }
      if (message.type === MESSAGE_TYPES.EVENT) {
        if (
          typeof message.event !== "string" ||
          message.params === null ||
          typeof message.params !== "object" ||
          Array.isArray(message.params)
        ) {
          this.failConnection("The local bridge sent an invalid event.");
          return;
        }
        await this.onEvent(message.event, message.params);
        return;
      }
      if (message.type === MESSAGE_TYPES.EXTENSION_RESET) {
        if (
          typeof message.epoch !== "string" ||
          message.epoch.length === 0 ||
          !Number.isInteger(message.generation) ||
          message.generation < 0
        ) {
          this.failConnection(
            "The local bridge sent an invalid reset request.",
          );
          return;
        }
        const result = await this.onReset(message.epoch, message.generation);
        if (result === null || result === undefined) {
          return;
        }
        if (
          typeof result.executed !== "boolean" ||
          typeof result.ok !== "boolean" ||
          !Number.isInteger(result.failedTabCount) ||
          result.failedTabCount < 0 ||
          (!result.executed && !result.ok) ||
          (result.ok && result.failedTabCount !== 0) ||
          (!result.ok && result.failedTabCount === 0)
        ) {
          this.failConnection(
            "The local bridge reset returned an invalid result.",
          );
          return;
        }
        const ack = {
          v: PROTOCOL_VERSION,
          type: MESSAGE_TYPES.EXTENSION_RESET_ACK,
          epoch: message.epoch,
          generation: message.generation,
          ok: result.ok,
        };
        if (!result.ok) {
          ack.failedTabCount = result.failedTabCount;
        }
        this.sendRaw(ack);
        return;
      }
      this.failConnection("The local bridge sent an unexpected message.");
    } catch (error) {
      if (
        error instanceof ProtocolError &&
        error.code === ERROR_CODES.AUTH_FAILED
      ) {
        this.failConnection(error.message, 4403);
      } else {
        this.failConnection("The local bridge connection failed.");
      }
    }
  }

  async handleAuthMessage(message) {
    if (
      message.type === MESSAGE_TYPES.AUTH_CHALLENGE &&
      this.serverNonce === null
    ) {
      const serverNonceBytes = base64UrlToBytes(message.serverNonce);
      if (serverNonceBytes.length !== 32) {
        throw new ProtocolError(
          ERROR_CODES.AUTH_FAILED,
          "Bridge authentication failed.",
        );
      }
      this.serverNonce = message.serverNonce;
      const nonceBytes = crypto.getRandomValues(new Uint8Array(32));
      this.clientNonce = bytesToBase64Url(nonceBytes);
      this.hmacKey = await importHmacKey(this.token);
      const proof = bytesToBase64Url(
        await signHmac(
          this.hmacKey,
          `${AUTH_EXT_CONTEXT}${this.serverNonce}|${this.clientNonce}`,
        ),
      );
      this.sendRaw({
        v: PROTOCOL_VERSION,
        type: MESSAGE_TYPES.AUTH_PROOF,
        clientNonce: this.clientNonce,
        proof,
      });
      return;
    }

    if (
      message.type === MESSAGE_TYPES.AUTH_OK &&
      this.clientNonce !== null &&
      this.serverNonce !== null
    ) {
      const receivedProof = base64UrlToBytes(message.serverProof);
      const expectedProof = await signHmac(
        this.hmacKey,
        `${AUTH_SERVER_CONTEXT}${this.clientNonce}|${this.serverNonce}`,
      );
      if (!constantTimeEqual(receivedProof, expectedProof)) {
        throw new ProtocolError(
          ERROR_CODES.AUTH_FAILED,
          "Bridge authentication failed.",
        );
      }
      this.authenticated = true;
      this.lastError = "";
      this.reconnectDelayMs = 1_000;
      this.notifyState();
      await this.onAuthenticated();
      return;
    }

    throw new ProtocolError(
      ERROR_CODES.AUTH_FAILED,
      "Bridge authentication failed.",
    );
  }

  async handleRequest(message) {
    if (typeof message.id !== "string" || typeof message.op !== "string") {
      this.failConnection("The local bridge sent an invalid request.");
      return;
    }

    try {
      const result = await this.onRequest(message.op, message.args);
      this.sendRaw({
        v: PROTOCOL_VERSION,
        type: MESSAGE_TYPES.RESPONSE,
        id: message.id,
        ok: true,
        result,
      });
    } catch (error) {
      this.sendRaw({
        v: PROTOCOL_VERSION,
        type: MESSAGE_TYPES.RESPONSE,
        id: message.id,
        ok: false,
        error: protocolErrorEnvelope(error),
      });
    }
  }

  sendEvent(event, params) {
    if (!this.authenticated) {
      return false;
    }
    return this.sendRaw({
      v: PROTOCOL_VERSION,
      type: MESSAGE_TYPES.EVENT,
      event,
      params,
    });
  }

  sendRaw(message) {
    if (this.socket?.readyState !== WebSocket.OPEN) {
      return false;
    }
    this.socket.send(JSON.stringify(message));
    return true;
  }

  normalizePort(value) {
    const port = Number(value);
    if (!Number.isInteger(port) || port < 1 || port > 65_535) {
      throw new ProtocolError(
        ERROR_CODES.INTERNAL,
        "Bridge port must be between 1 and 65535.",
      );
    }
    return port;
  }

  startHealthTimers() {
    this.stopHealthTimers();
    this.pingTimer = setInterval(() => {
      if (this.authenticated) {
        this.sendRaw({ v: PROTOCOL_VERSION, type: MESSAGE_TYPES.PING });
      }
    }, PING_INTERVAL_MS);
    this.silenceTimer = setInterval(() => {
      if (
        this.lastMessageAt > 0 &&
        Date.now() - this.lastMessageAt > SILENCE_TIMEOUT_MS
      ) {
        this.failConnection("The local bridge stopped responding.");
      }
    }, 5_000);
  }

  stopHealthTimers() {
    if (this.pingTimer !== null) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.silenceTimer !== null) {
      clearInterval(this.silenceTimer);
      this.silenceTimer = null;
    }
  }

  scheduleReconnect() {
    if (!this.enabled || !this.token || this.reconnectTimer !== null) {
      return;
    }
    const delay = this.reconnectDelayMs;
    this.reconnectDelayMs = Math.min(
      this.reconnectDelayMs * 2,
      RECONNECT_MAX_MS,
    );
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.kick();
    }, delay);
  }

  clearReconnectTimer() {
    if (this.reconnectTimer !== null) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  clearConnectTimer() {
    if (this.connectTimer !== null) {
      clearTimeout(this.connectTimer);
      this.connectTimer = null;
    }
  }

  closeSocket(code = 1000, reason = "Disconnected") {
    this.connectionGeneration += 1;
    const socket = this.socket;
    this.socket = null;
    this.authenticated = false;
    this.clearConnectTimer();
    this.stopHealthTimers();
    if (socket !== null && socket.readyState < WebSocket.CLOSING) {
      socket.close(code, reason);
    }
    this.notifyState();
  }

  failConnection(message, code = 4400) {
    this.lastError = message;
    this.closeSocket(code, message.slice(0, 120));
    this.scheduleReconnect();
  }

  notifyState() {
    this.onStateChange(this.getStatus());
  }
}
