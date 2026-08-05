import {
  ALLOWED_CDP_PREFIXES,
  DENIED_CDP_METHODS,
  ERROR_CODES,
  EVENTS,
  ProtocolError,
  requireArgs,
  requireTabId,
} from "./protocol.js";

export class DebuggerRouter {
  constructor({ tabScope, sendEvent, onAttachedChange }) {
    this.tabScope = tabScope;
    this.sendEvent = sendEvent;
    this.onAttachedChange = onAttachedChange;
    this.attachedTabs = new Set();

    chrome.debugger.onEvent.addListener((source, method, params) => {
      void this.handleDebuggerEvent(source, method, params);
    });
    chrome.debugger.onDetach.addListener((source, reason) => {
      void this.handleDebuggerDetach(source, reason);
    });
  }

  hasAttachedTabs() {
    return this.attachedTabs.size > 0;
  }

  async attach(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async () => {
      await this.ensureStillControllableLocked(tabId);
      if (this.attachedTabs.has(tabId)) {
        return {};
      }
      try {
        await chrome.debugger.attach({ tabId }, "1.3");
      } catch {
        await this.ensureStillControllableLocked(tabId);
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "Chrome could not attach the debugger to this tab.",
        );
      }
      this.attachedTabs.add(tabId);
      this.onAttachedChange();
      await this.ensureStillControllableLocked(tabId);
      return {};
    });
  }

  async detach(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async () => {
      await this.tabScope.assertScoped(tabId);
      if (!this.attachedTabs.has(tabId)) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      try {
        await chrome.debugger.detach({ tabId });
      } catch {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      this.attachedTabs.delete(tabId);
      this.onAttachedChange();
      return {};
    });
  }

  async send(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async () => {
      await this.ensureStillControllableLocked(tabId);
      if (!this.attachedTabs.has(tabId)) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      if (
        typeof values.method !== "string" ||
        !this.isMethodAllowed(values.method, values.params)
      ) {
        throw new ProtocolError(
          ERROR_CODES.CDP_METHOD_NOT_ALLOWED,
          "The requested CDP method is not allowed.",
        );
      }
      if (
        values.sessionId !== undefined &&
        typeof values.sessionId !== "string"
      ) {
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "The CDP session identifier is invalid.",
        );
      }
      if (
        values.params !== undefined &&
        (values.params === null ||
          typeof values.params !== "object" ||
          Array.isArray(values.params))
      ) {
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "CDP parameters must be an object.",
        );
      }

      const target =
        values.sessionId === undefined
          ? { tabId }
          : { tabId, sessionId: values.sessionId };
      let result;
      try {
        result = await chrome.debugger.sendCommand(
          target,
          values.method,
          values.params ?? {},
        );
      } catch {
        await this.ensureStillControllableLocked(tabId);
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "Chrome rejected the CDP command.",
        );
      }
      await this.ensureStillControllableLocked(tabId);
      return { result: result ?? {} };
    });
  }

  async detachIfAttached(tabId) {
    return this.tabScope.runTabOperation(tabId, () =>
      this.detachIfAttachedLocked(tabId),
    );
  }

  async detachIfAttachedLocked(tabId) {
    if (!this.attachedTabs.has(tabId)) {
      return;
    }
    try {
      await chrome.debugger.detach({ tabId });
    } catch {
      this.attachedTabs.delete(tabId);
      this.onAttachedChange();
      return;
    }
    this.attachedTabs.delete(tabId);
    this.onAttachedChange();
  }

  async ensureStillControllableLocked(tabId) {
    try {
      await this.tabScope.assertControllableLocked(tabId);
    } catch (error) {
      await this.detachIfAttachedLocked(tabId);
      throw error;
    }
  }

  isMethodAllowed(method, params) {
    if (
      DENIED_CDP_METHODS.has(method) ||
      (method === "Network.getCookies" &&
        params !== null &&
        typeof params === "object" &&
        Object.prototype.hasOwnProperty.call(params, "urls"))
    ) {
      return false;
    }
    const separator = method.indexOf(".");
    return (
      separator > 0 && ALLOWED_CDP_PREFIXES.has(method.slice(0, separator))
    );
  }

  async handleDebuggerEvent(source, method, params) {
    if (
      !Number.isInteger(source.tabId) ||
      !this.tabScope.isScoped(source.tabId)
    ) {
      return;
    }
    const eventParams = { tabId: source.tabId, method, params: params ?? {} };
    if (typeof source.sessionId === "string") {
      eventParams.sessionId = source.sessionId;
    }
    this.sendEvent(EVENTS.DEBUGGER_EVENT, eventParams);
  }

  async handleDebuggerDetach(source, reason) {
    if (!Number.isInteger(source.tabId)) {
      return;
    }
    await this.tabScope.runTabOperation(source.tabId, async () => {
      this.attachedTabs.delete(source.tabId);
      this.onAttachedChange();
      this.sendEvent(EVENTS.DEBUGGER_DETACHED, {
        tabId: source.tabId,
        reason: typeof reason === "string" ? reason : "unknown",
      });
      await this.tabScope.handleDebuggerDetachLocked(source.tabId);
    });
  }
}
