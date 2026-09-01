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
const ATTACH_TIMEOUT_MS = 10_000;
const COMMAND_TIMEOUT_MS = 25_000;
const RECOVERY_TIMEOUT_MS = 2_000;
const OPERATION_DEADLINE_MARGIN_MS = 500;
const LEAF_TARGET_TYPES = new Set([
  "service_worker",
  "shared_worker",
  "worker",
]);
const URL_CHANGING_CDP_METHODS = new Set([
  "Page.navigate",
  "Page.navigateToHistoryEntry",
  "Page.reload",
]);

function debuggerErrorReason(error) {
  const reason = error instanceof Error ? error.message.trim() : "";
  return reason ? reason.slice(0, 200) : "unknown reason";
}

export class DebuggerRouter {
  constructor({
    tabScope,
    sendEvent,
    onAttachedChange,
    attachTimeoutMs = ATTACH_TIMEOUT_MS,
    commandTimeoutMs = COMMAND_TIMEOUT_MS,
    recoveryTimeoutMs = RECOVERY_TIMEOUT_MS,
    operationDeadlineMarginMs = OPERATION_DEADLINE_MARGIN_MS,
  }) {
    this.tabScope = tabScope;
    this.sendEvent = sendEvent;
    this.onAttachedChange = onAttachedChange;
    this.attachedTabs = new Set();
    this.attachStates = new Map();
    this.childTargets = new Map();
    this.attachTimeoutMs = attachTimeoutMs;
    this.commandTimeoutMs = commandTimeoutMs;
    this.recoveryTimeoutMs = recoveryTimeoutMs;
    this.operationDeadlineMarginMs = operationDeadlineMarginMs;
    this.stateSequence = 0;

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
    const attachedTabIds = [
      ...new Set([...this.attachedTabs, ...this.attachStates.keys()]),
    ];
    const results = await Promise.all(
      attachedTabIds.map(async (tabId) => {
        const priorState = this.attachStates.get(tabId);
        if (!this.attachedTabs.has(tabId) && priorState?.status === "failed") {
          this.attachStates.delete(tabId);
          return true;
        }
        if (
          !this.attachedTabs.has(tabId) &&
          ["attaching", "orphaned_attach"].includes(priorState?.status)
        ) {
          this.attachStates.set(tabId, {
            ...priorState,
            status: "orphaned_attach",
          });
          if (!(await this.isDebuggerStillAttached(tabId))) {
            return true;
          }
        }
        const token = priorState?.token ?? this.nextStateToken();
        this.attachStates.set(tabId, { status: "detaching", token });
        const detachPromise = Promise.resolve().then(() =>
          chrome.debugger.detach({ tabId }),
        );
        try {
          await this.withTimeout(() => detachPromise, this.recoveryTimeoutMs);
        } catch {
          this.trackLateDetach(tabId, token, detachPromise);
          if (await this.isDebuggerStillAttached(tabId)) {
            this.quarantineTab(tabId, token, "detach_failed");
            return false;
          }
        }
        this.clearDetachedState(tabId, token);
        return true;
      }),
    );
    for (const [tabId, state] of this.attachStates) {
      if (state.status === "failed" && !this.attachedTabs.has(tabId)) {
        this.attachStates.delete(tabId);
      }
    }
    this.onAttachedChange();
    return { failedTabCount: results.filter((detached) => !detached).length };
  }

  async isDebuggerStillAttached(tabId) {
    try {
      const targets = await this.withTimeout(
        () => chrome.debugger.getTargets(),
        this.recoveryTimeoutMs,
      );
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
    return this.tabScope.runTabOperation(tabId, async (lease) => {
      await this.ensureStillControllableLocked(tabId, lease);
      if (this.attachedTabs.has(tabId)) {
        this.attachStates.set(tabId, { status: "attached" });
        return {};
      }
      const existingState = this.attachStates.get(tabId);
      if (existingState !== undefined) {
        throw new ProtocolError(
          ERROR_CODES.ATTACH_FAILED,
          existingState.reason ||
            "A debugger attach attempt is already unresolved.",
        );
      }
      const token = this.nextStateToken();
      const attachPromise = Promise.resolve().then(() =>
        chrome.debugger.attach({ tabId }, "1.3"),
      );
      this.attachStates.set(tabId, { status: "attaching", token });
      let timedOut = false;
      try {
        await this.withTimeout(
          () => attachPromise,
          this.attachTimeoutMs,
          () => {
            timedOut = true;
            return new ProtocolError(
              ERROR_CODES.ATTACH_FAILED,
              "Chrome debugger attach timed out.",
            );
          },
        );
      } catch (error) {
        const attachError =
          error instanceof ProtocolError &&
          error.code === ERROR_CODES.ATTACH_FAILED
            ? error
            : new ProtocolError(
                ERROR_CODES.ATTACH_FAILED,
                `Chrome rejected debugger attachment: ${debuggerErrorReason(error)}`,
              );
        if (timedOut) {
          this.attachStates.set(tabId, {
            status: "orphaned_attach",
            token,
            reason: attachError.message,
          });
          this.trackLateAttach(tabId, token, attachPromise);
        } else if (this.attachStates.get(tabId)?.token === token) {
          if (lease.isCurrent()) {
            this.attachStates.set(tabId, {
              status: "failed",
              token,
              reason: attachError.message,
            });
          } else {
            this.attachStates.delete(tabId);
          }
        }
        throw attachError;
      }
      if (
        !lease.isCurrent() ||
        this.attachStates.get(tabId)?.token !== token ||
        this.attachStates.get(tabId)?.status !== "attaching"
      ) {
        this.attachStates.set(tabId, {
          status: "orphaned_attach",
          token,
          reason: "attach_cancelled",
        });
        this.trackLateAttach(tabId, token, Promise.resolve());
        lease.assertCurrent();
      }
      this.attachedTabs.add(tabId);
      this.attachStates.set(tabId, { status: "attached", token });
      this.onAttachedChange();
      await this.ensureStillControllableLocked(tabId, lease);
      return {};
    });
  }

  async detach(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async (lease) => {
      await this.tabScope.assertScoped(tabId);
      if (!this.attachedTabs.has(tabId)) {
        await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
        return {};
      }
      if (
        this.attachStates.has(tabId) &&
        this.attachStates.get(tabId)?.status !== "attached"
      ) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      const token = this.nextStateToken();
      const detachPromise = Promise.resolve().then(() =>
        chrome.debugger.detach({ tabId }),
      );
      this.attachStates.set(tabId, { status: "detaching", token });
      let timedOut = false;
      try {
        await this.withTimeout(
          () => detachPromise,
          this.recoveryTimeoutMs,
          () => {
            timedOut = true;
            return new Error("timeout");
          },
        );
      } catch {
        if (timedOut) {
          this.quarantineTab(tabId, token, "detach_timeout");
          this.trackLateDetach(tabId, token, detachPromise);
        } else if (await this.isDebuggerStillAttached(tabId)) {
          this.attachStates.set(tabId, { status: "attached", token });
          throw new ProtocolError(
            ERROR_CODES.CDP_ERROR,
            "Chrome could not detach the debugger from this tab.",
          );
        } else {
          this.clearDetachedState(tabId, token);
          await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
          return {};
        }
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      this.clearDetachedState(tabId, token);
      await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
      return {};
    });
  }

  async send(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async (lease) => {
      await this.ensureStillControllableLocked(tabId, lease);
      if (
        !this.attachedTabs.has(tabId) ||
        this.attachStates.get(tabId)?.status !== "attached"
      ) {
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
      let urlChangeGranted = false;
      if (URL_CHANGING_CDP_METHODS.has(values.method)) {
        // Page.navigate binds the grant to its target URL; history/reload
        // navigations have no knowable target, so their grant matches any
        // single URL change.
        lease.allowUrlChange(
          values.method === "Page.navigate" &&
            typeof values.params?.url === "string"
            ? values.params.url
            : null,
        );
        urlChangeGranted = true;
      }
      const childTarget = this.childTargets.get(values.sessionId);
      if (
        values.method === "Target.setAutoAttach" &&
        childTarget?.tabId === tabId &&
        LEAF_TARGET_TYPES.has(childTarget?.type)
      ) {
        return { result: {} };
      }

      const target =
        values.sessionId === undefined
          ? { tabId }
          : { tabId, sessionId: values.sessionId };
      const childAutoAttach =
        values.method === "Target.setAutoAttach" &&
        values.sessionId !== undefined;
      const commandTimeoutMs = this.commandTimeoutWithinLease(
        lease,
        childAutoAttach ? CHILD_AUTO_ATTACH_TIMEOUT_MS : this.commandTimeoutMs,
        childAutoAttach ? 0 : this.recoveryTimeoutMs * 2,
        values.method,
      );
      let result;
      try {
        result = await (childAutoAttach
          ? this.sendCommandWithTimeout(
              target,
              values.method,
              values.params ?? {},
              commandTimeoutMs,
            )
          : this.sendCommandWithTimeout(
              target,
              values.method,
              values.params ?? {},
              commandTimeoutMs,
            ));
      } catch (error) {
        if (urlChangeGranted) {
          lease.revokeUrlChange();
        }
        if (
          error instanceof ProtocolError &&
          error.code === ERROR_CODES.COMMAND_TIMEOUT
        ) {
          if (lease.isCurrent() && !childAutoAttach) {
            await this.recoverTimedOutCommandLocked(tabId);
          }
          throw error;
        }
        await this.ensureStillControllableLocked(tabId, lease);
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "Chrome rejected the CDP command.",
        );
      }
      lease.assertCurrent();
      await this.ensureStillControllableLocked(tabId, lease);
      if (
        !this.attachedTabs.has(tabId) ||
        this.attachStates.get(tabId)?.status !== "attached"
      ) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
      return { result: result ?? {} };
    });
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
    return this.withTimeout(
      () => chrome.debugger.sendCommand(target, method, params),
      timeoutMs,
      () =>
        new ProtocolError(
          ERROR_CODES.COMMAND_TIMEOUT,
          `Chrome debugger command timed out: ${method}`,
        ),
    );
  }

  commandTimeoutWithinLease(
    lease,
    requestedTimeoutMs,
    recoveryReserveMs,
    method,
  ) {
    if (typeof lease.remainingMs !== "function") {
      return requestedTimeoutMs;
    }
    const availableMs = Math.floor(
      lease.remainingMs() - recoveryReserveMs - this.operationDeadlineMarginMs,
    );
    if (availableMs <= 0) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        `Chrome debugger command timed out before it could start: ${method}`,
      );
    }
    return Math.min(requestedTimeoutMs, availableMs);
  }

  async withTimeout(
    operation,
    timeoutMs,
    timeoutError = () => new Error("timeout"),
  ) {
    let timeoutId;
    try {
      return await Promise.race([
        Promise.resolve().then(operation),
        new Promise((_, reject) => {
          timeoutId = setTimeout(() => reject(timeoutError()), timeoutMs);
        }),
      ]);
    } finally {
      clearTimeout(timeoutId);
    }
  }

  async recoverTimedOutCommandLocked(tabId) {
    const token = this.nextStateToken();
    this.attachStates.set(tabId, { status: "recovering", token });
    const detachPromise = Promise.resolve().then(() =>
      chrome.debugger.detach({ tabId }),
    );
    let timedOut = false;
    try {
      await this.withTimeout(
        () => detachPromise,
        this.recoveryTimeoutMs,
        () => {
          timedOut = true;
          return new Error("timeout");
        },
      );
      this.clearDetachedState(tabId, token);
    } catch {
      if (timedOut) {
        this.quarantineTab(tabId, token, "command_timeout");
        this.trackLateDetach(tabId, token, detachPromise);
      } else if (await this.isDebuggerStillAttached(tabId)) {
        this.quarantineTab(tabId, token, "command_timeout_detach_failed");
      } else {
        this.clearDetachedState(tabId, token);
      }
    }
  }

  async detachIfAttached(tabId) {
    return this.tabScope.runTabOperation(tabId, () =>
      this.detachIfAttachedLocked(tabId),
    );
  }

  async detachIfAttachedLocked(tabId) {
    if (!this.attachedTabs.has(tabId)) {
      if (
        !["attaching", "orphaned_attach"].includes(
          this.attachStates.get(tabId)?.status,
        )
      ) {
        this.attachStates.delete(tabId);
      }
      return;
    }
    const token = this.nextStateToken();
    const detachPromise = Promise.resolve().then(() =>
      chrome.debugger.detach({ tabId }),
    );
    this.attachStates.set(tabId, { status: "detaching", token });
    let timedOut = false;
    try {
      await this.withTimeout(
        () => detachPromise,
        this.recoveryTimeoutMs,
        () => {
          timedOut = true;
          return new Error("timeout");
        },
      );
    } catch {
      if (timedOut) {
        this.quarantineTab(tabId, token, "detach_timeout");
        this.trackLateDetach(tabId, token, detachPromise);
      } else if (!(await this.isDebuggerStillAttached(tabId))) {
        this.clearDetachedState(tabId, token);
      } else {
        this.quarantineTab(tabId, token, "detach_failed");
      }
      return;
    }
    this.clearDetachedState(tabId, token);
  }

  nextStateToken() {
    this.stateSequence += 1;
    return this.stateSequence;
  }

  clearDetachedState(tabId, token = null) {
    if (token !== null && this.attachStates.get(tabId)?.token !== token) {
      return;
    }
    this.attachedTabs.delete(tabId);
    this.attachStates.delete(tabId);
    this.forgetChildTargets(tabId);
    this.onAttachedChange();
  }

  quarantineTab(tabId, token, reason) {
    this.attachedTabs.delete(tabId);
    this.attachStates.set(tabId, { status: "quarantined", token, reason });
    this.forgetChildTargets(tabId);
    this.onAttachedChange();
  }

  trackLateAttach(tabId, token, attachPromise) {
    void attachPromise
      .then(
        () => this.compensateLateAttach(tabId, token),
        () => this.clearLateAttachFailure(tabId, token),
      )
      .catch(() => undefined);
  }

  async compensateLateAttach(tabId, token) {
    await this.tabScope.runTabOperation(tabId, async () => {
      if (this.attachStates.get(tabId)?.token !== token) {
        return;
      }
      const detachPromise = Promise.resolve().then(() =>
        chrome.debugger.detach({ tabId }),
      );
      this.attachStates.set(tabId, { status: "detaching", token });
      let timedOut = false;
      try {
        await this.withTimeout(
          () => detachPromise,
          this.recoveryTimeoutMs,
          () => {
            timedOut = true;
            return new Error("timeout");
          },
        );
        this.clearDetachedState(tabId, token);
      } catch {
        if (timedOut) {
          this.quarantineTab(tabId, token, "late_attach_detach_timeout");
          this.trackLateDetach(tabId, token, detachPromise);
        } else if (await this.isDebuggerStillAttached(tabId)) {
          this.quarantineTab(tabId, token, "late_attach_detach_failed");
        } else {
          this.clearDetachedState(tabId, token);
        }
      }
    });
  }

  async clearLateAttachFailure(tabId, token) {
    await this.tabScope.runTabOperation(tabId, async () => {
      if (
        this.attachStates.get(tabId)?.token === token &&
        !this.attachedTabs.has(tabId)
      ) {
        this.attachStates.delete(tabId);
      }
    });
  }

  trackLateDetach(tabId, token, detachPromise) {
    void detachPromise
      .then(
        () =>
          this.tabScope.runTabOperation(tabId, async () => {
            this.clearDetachedState(tabId, token);
          }),
        () =>
          this.tabScope.runTabOperation(tabId, async () => {
            if (this.attachStates.get(tabId)?.token !== token) {
              return;
            }
            if (await this.isDebuggerStillAttached(tabId)) {
              this.quarantineTab(tabId, token, "detach_failed");
            } else {
              this.clearDetachedState(tabId, token);
            }
          }),
      )
      .catch(() => undefined);
  }

  async ensureStillControllableLocked(tabId, lease = null) {
    try {
      await this.tabScope.assertControllableLocked(tabId, lease);
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
      !this.tabScope.isScoped(source.tabId) ||
      this.tabScope.isQuarantined?.(source.tabId) === true
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
    await this.tabScope.runTabOperation(source.tabId, async (lease) => {
      lease.assertCurrent();
      this.attachedTabs.delete(source.tabId);
      this.attachStates.delete(source.tabId);
      this.forgetChildTargets(source.tabId);
      this.onAttachedChange();
      this.sendEvent(EVENTS.DEBUGGER_DETACHED, {
        tabId: source.tabId,
        reason: typeof reason === "string" ? reason : "unknown",
      });
      await this.tabScope.handleDebuggerDetachLocked(source.tabId, lease);
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
