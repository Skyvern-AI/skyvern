import {
  ALLOWED_CDP_PREFIXES,
  DENIED_CDP_METHODS,
  ERROR_CODES,
  EVENTS,
  ProtocolError,
  requireArgs,
  requireTabId,
} from "./protocol.js";

const CHILD_AUTO_ATTACH_TIMEOUT_MS = 2_000;
const LEAF_TARGET_TYPES = new Set([
  "service_worker",
  "shared_worker",
  "worker",
]);

export class DebuggerRouter {
  constructor({ tabScope, sendEvent, onAttachedChange }) {
    this.tabScope = tabScope;
    this.sendEvent = sendEvent;
    this.onAttachedChange = onAttachedChange;
    this.attachedTabs = new Set();
    this.childTargets = new Map();

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

  async reset() {
    const attachedTabIds = [...this.attachedTabs];
    const results = await Promise.all(
      attachedTabIds.map(async (tabId) => {
        try {
          await chrome.debugger.detach({ tabId });
        } catch {
          if (await this.isDebuggerStillAttached(tabId)) {
            return false;
          }
        }
        this.attachedTabs.delete(tabId);
        this.forgetChildTargets(tabId);
        return true;
      }),
    );
    this.onAttachedChange();
    return { failedTabCount: results.filter((detached) => !detached).length };
  }

  async isDebuggerStillAttached(tabId) {
    try {
      const targets = await chrome.debugger.getTargets();
      return targets.some(
        (target) => target.tabId === tabId && target.attached,
      );
    } catch {
      return true;
    }
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
        await this.tabScope.handleDebuggerDetachLocked(tabId);
        return {};
      }
      try {
        await chrome.debugger.detach({ tabId });
      } catch {
        if (await this.isDebuggerStillAttached(tabId)) {
          throw new ProtocolError(
            ERROR_CODES.CDP_ERROR,
            "Chrome could not detach the debugger from this tab.",
          );
        }
      }
      this.attachedTabs.delete(tabId);
      this.forgetChildTargets(tabId);
      this.onAttachedChange();
      await this.tabScope.handleDebuggerDetachLocked(tabId);
      return {};
    });
  }

  async send(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    let commandPromise;
    let commandError;
    let skippedLeafAutoAttach = false;
    await this.tabScope.runTabOperation(tabId, async () => {
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
      const childTarget = this.childTargets.get(values.sessionId);
      if (
        values.method === "Target.setAutoAttach" &&
        childTarget?.tabId === tabId &&
        LEAF_TARGET_TYPES.has(childTarget?.type)
      ) {
        skippedLeafAutoAttach = true;
        return;
      }

      const target =
        values.sessionId === undefined
          ? { tabId }
          : { tabId, sessionId: values.sessionId };
      // Initiate under the per-tab chain, then release it before awaiting the response.
      try {
        commandPromise =
          values.method === "Target.setAutoAttach" &&
          values.sessionId !== undefined
            ? this.sendCommandWithTimeout(
                target,
                values.method,
                values.params ?? {},
                CHILD_AUTO_ATTACH_TIMEOUT_MS,
              )
            : chrome.debugger.sendCommand(
                target,
                values.method,
                values.params ?? {},
              );
      } catch (error) {
        commandError = error;
      }
    });
    if (skippedLeafAutoAttach) {
      return { result: {} };
    }

    let result;
    try {
      if (commandError !== undefined) {
        throw commandError;
      }
      result = await commandPromise;
    } catch {
      await this.assertCanSend(tabId);
      throw new ProtocolError(
        ERROR_CODES.CDP_ERROR,
        "Chrome rejected the CDP command.",
      );
    }
    await this.assertCanSend(tabId);
    return { result: result ?? {} };
  }

  async assertCanSend(tabId) {
    return this.tabScope.runTabOperation(tabId, async () => {
      await this.ensureStillControllableLocked(tabId);
      if (!this.attachedTabs.has(tabId)) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
    });
  }

  async sendCommandWithTimeout(target, method, params, timeoutMs) {
    let timeoutId;
    try {
      return await Promise.race([
        chrome.debugger.sendCommand(target, method, params),
        new Promise((_, reject) => {
          timeoutId = setTimeout(() => reject(new Error("timeout")), timeoutMs);
        }),
      ]);
    } finally {
      clearTimeout(timeoutId);
    }
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
      this.forgetChildTargets(tabId);
      this.onAttachedChange();
      return;
    }
    this.attachedTabs.delete(tabId);
    this.forgetChildTargets(tabId);
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
    const childSessionId = params?.sessionId;
    if (
      method === "Target.attachedToTarget" &&
      typeof childSessionId === "string" &&
      params.targetInfo !== null &&
      typeof params.targetInfo === "object"
    ) {
      this.childTargets.set(childSessionId, {
        tabId: source.tabId,
        type: params.targetInfo.type,
      });
    } else if (
      method === "Target.detachedFromTarget" &&
      typeof childSessionId === "string"
    ) {
      this.childTargets.delete(childSessionId);
    }
    this.sendEvent(EVENTS.DEBUGGER_EVENT, eventParams);
  }

  async handleDebuggerDetach(source, reason) {
    if (!Number.isInteger(source.tabId)) {
      return;
    }
    await this.tabScope.runTabOperation(source.tabId, async () => {
      this.attachedTabs.delete(source.tabId);
      this.forgetChildTargets(source.tabId);
      this.onAttachedChange();
      this.sendEvent(EVENTS.DEBUGGER_DETACHED, {
        tabId: source.tabId,
        reason: typeof reason === "string" ? reason : "unknown",
      });
      await this.tabScope.handleDebuggerDetachLocked(source.tabId);
    });
  }

  forgetChildTargets(tabId) {
    for (const [sessionId, target] of this.childTargets) {
      if (target.tabId === tabId) {
        this.childTargets.delete(sessionId);
      }
    }
  }
}
