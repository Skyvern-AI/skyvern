import {
  ALLOWED_CDP_PREFIXES,
  DENIED_CDP_METHODS,
  ERROR_CODES,
  EVENTS,
  ProtocolError,
  isPageChangeExempt,
  isRestrictedUrl,
  requireArgs,
  requireTabId,
} from "./protocol.js";

export const FOREIGN_FRAME_RECOVERY_LIMITS = Object.freeze({
  recoveries: 8,
  windowMs: 10_000,
  attempts: 3,
  attachTimeoutMs: 2_000,
  deadlineMs: 3_000,
  retryDelayMs: 100,
  removalTimeoutMs: 300,
  removalExpiryMs: 500,
});

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
    isIndicatorVisible = () => false,
    sendIndicatorMessage = async () => undefined,
    removeForeignFrames = async () => 0,
    isTransportConnected = () => false,
    getTransportGeneration = () => 0,
    attachTimeoutMs = ATTACH_TIMEOUT_MS,
    commandTimeoutMs = COMMAND_TIMEOUT_MS,
    recoveryTimeoutMs = RECOVERY_TIMEOUT_MS,
    operationDeadlineMarginMs = OPERATION_DEADLINE_MARGIN_MS,
  }) {
    this.tabScope = tabScope;
    this.sendEvent = sendEvent;
    this.onAttachedChange = onAttachedChange;
    this.isIndicatorVisible = isIndicatorVisible;
    this.sendIndicatorMessage = sendIndicatorMessage;
    this.removeForeignFrames = removeForeignFrames;
    this.isTransportConnected = isTransportConnected;
    this.getTransportGeneration = getTransportGeneration;
    this.commandFences = new Map();
    this.detachSequences = new Map();
    this.attachArrivalSequence = 0;
    this.stateTokens = new Map();
    this.recoveries = new Map();
    this.recoverySequence = 0;
    this.recoveryStarts = new Map();
    this.finishTransportRelease = null;
    this.captureReleases = new Map();
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
      void this.handleDebuggerDetach(source, reason).catch(() => undefined);
      this.releaseCaptures(source.tabId);
    });
  }

  hasAttachedTabs() {
    return this.attachedTabs.size > 0;
  }

  async reset() {
    const finishRelease = this.beginReleaseAll();
    this.commandFences.clear();
    this.recoveries.clear();
    try {
      const attachedTabIds = [
        ...new Set([...this.attachedTabs, ...this.attachStates.keys()]),
      ];
      const results = await Promise.all(
        attachedTabIds.map(async (tabId) => {
          const priorState = this.attachStates.get(tabId);
          if (
            !this.attachedTabs.has(tabId) &&
            priorState?.status === "failed"
          ) {
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
          const token = priorState?.token ?? this.nextStateToken(tabId);
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
          this.clearDetachedState(tabId, token, "reset");
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
    } finally {
      finishRelease();
    }
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
    const arrivalSequence = ++this.attachArrivalSequence;
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.tabScope.runTabOperation(tabId, async (lease) => {
      await this.ensureStillControllableLocked(tabId, lease);
      if (this.attachedTabs.has(tabId)) {
        this.liftCommandFence(tabId, arrivalSequence);
        return {};
      }
      this.assertNotFenced(tabId);
      const existingState = this.attachStates.get(tabId);
      if (existingState !== undefined) {
        throw new ProtocolError(
          ERROR_CODES.ATTACH_FAILED,
          existingState.reason ||
            "A debugger attach attempt is already unresolved.",
        );
      }
      const token = this.nextStateToken(tabId);
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
      this.liftCommandFence(tabId, arrivalSequence);
      return {};
    });
  }

  async detach(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    const release = this.tabScope.beginRelease?.(tabId);
    this.cancelRecovery(tabId);
    try {
      return await this.tabScope.runTabOperation(tabId, async (lease) => {
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
        const token = this.nextStateToken(tabId);
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
            this.clearDetachedState(tabId, token, "detached");
            await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
            return {};
          }
          throw new ProtocolError(
            ERROR_CODES.DEBUGGER_DETACHED,
            "The debugger is not attached to this tab.",
          );
        }
        this.clearDetachedState(tabId, token, "detached");
        await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
        return {};
      });
    } finally {
      this.tabScope.endRelease?.(tabId, release);
    }
  }

  async send(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    const detachSequence = this.detachSequences.get(tabId);
    this.assertNotFenced(tabId, detachSequence);
    const sendCommand = async (lease) => {
      this.assertNotFenced(tabId, detachSequence);
      await this.ensureStillControllableLocked(tabId, lease);
      this.assertNotFenced(tabId, detachSequence);
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
        lease.allowUrlChange();
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
        const release =
          values.method === "Page.captureScreenshot" &&
          this.isIndicatorVisible(tabId)
            ? await this.suppressIndicator(tabId)
            : undefined;
        this.assertNotFenced(tabId, detachSequence);
        result = await this.sendCommandWithTimeout(
          target,
          values.method,
          values.params ?? {},
          commandTimeoutMs,
          lease,
          release,
        );
      } catch (error) {
        this.assertNotFenced(tabId, detachSequence);
        if (urlChangeGranted) {
          lease.revokeUrlChange();
        }
        if (
          error instanceof ProtocolError &&
          error.code === ERROR_CODES.COMMAND_TIMEOUT
        ) {
          // Child frames can legitimately never answer, e.g. while navigating away.
          if (
            error.commandTimedOut === true &&
            values.sessionId === undefined
          ) {
            await this.recoverTimedOutCommandLocked(tabId);
          }
          throw error;
        }
        await this.ensureStillControllableLocked(tabId, lease);
        this.assertNotFenced(tabId, detachSequence);
        throw new ProtocolError(
          ERROR_CODES.CDP_ERROR,
          "Chrome rejected the CDP command.",
        );
      }
      lease.assertCurrent();
      await this.ensureStillControllableLocked(tabId, lease);
      this.assertNotFenced(tabId, detachSequence);
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
    };
    const classifyLease = (lease) => {
      lease.debuggerCommand = true;
      lease.dispatched = false;
      lease.pageChangeExempt = isPageChangeExempt(
        values.method,
        values.params ?? {},
      );
    };
    return this.tabScope.runTabOperation(
      tabId,
      sendCommand,
      this.tabScope.operationGeneration,
      true,
      classifyLease,
    );
  }

  assertNotFenced(tabId, detachSequence = this.detachSequences.get(tabId)) {
    if (
      this.commandFences.has(tabId) ||
      this.detachSequences.get(tabId) !== detachSequence
    ) {
      throw new ProtocolError(
        ERROR_CODES.DEBUGGER_DETACHED,
        "The debugger is not attached to this tab.",
      );
    }
  }

  clearCommandFence(tabId) {
    this.commandFences.delete(tabId);
  }

  liftCommandFence(tabId, arrivalSequence) {
    const fence = this.commandFences.get(tabId);
    if (fence?.announcedAt !== null && arrivalSequence > fence?.announcedAt) {
      this.clearCommandFence(tabId);
    }
  }

  async assertCanSend(tabId, { fenceOnly = false } = {}) {
    const detachSequence = this.detachSequences.get(tabId);
    this.assertNotFenced(tabId, detachSequence);
    if (fenceOnly) return;
    return this.tabScope.runTabOperation(tabId, async (currentLease) => {
      await this.ensureStillControllableLocked(tabId, currentLease);
      this.assertNotFenced(tabId, detachSequence);
      if (!this.attachedTabs.has(tabId)) {
        throw new ProtocolError(
          ERROR_CODES.DEBUGGER_DETACHED,
          "The debugger is not attached to this tab.",
        );
      }
    });
  }

  getCaptureTokens(tabId) {
    return [...(this.captureReleases.get(tabId)?.keys() ?? [])];
  }

  async suppressIndicator(tabId) {
    const token = crypto.randomUUID();
    const releases = this.captureReleases.get(tabId) ?? new Map();
    this.captureReleases.set(tabId, releases);
    const release = () => {
      if (!releases.delete(token)) return;
      if (releases.size === 0) this.captureReleases.delete(tabId);
      void this.messageIndicator(tabId, {
        type: "skyvern.indicator.release",
        token,
      });
    };
    releases.set(token, release);
    try {
      await this.withTimeout(
        () =>
          this.messageIndicator(tabId, {
            type: "skyvern.indicator.suppress",
            token,
          }),
        250,
      );
    } catch {}
    return release;
  }

  async messageIndicator(tabId, message) {
    try {
      return await this.sendIndicatorMessage(tabId, message);
    } catch {}
  }

  releaseCaptures(tabId) {
    for (const release of this.captureReleases.get(tabId)?.values() ?? [])
      release();
  }

  async sendCommandWithTimeout(
    target,
    method,
    params,
    timeoutMs,
    lease = null,
    onSettled,
  ) {
    const detachSequence = this.detachSequences.get(target.tabId);
    let timedOut = false;
    const command = this.withTimeout(
      () => {
        try {
          lease?.assertCurrent();
          this.assertNotFenced(target.tabId, detachSequence);
          if (lease) lease.dispatched = true;
          const pending = chrome.debugger.sendCommand(target, method, params);
          // The timeout/lease race can end before Chrome has sampled the pixels.
          return onSettled
            ? Promise.resolve(pending).finally(onSettled)
            : pending;
        } catch (error) {
          onSettled?.();
          throw error;
        }
      },
      timeoutMs,
      () => {
        timedOut = true;
        const error = new ProtocolError(
          ERROR_CODES.COMMAND_TIMEOUT,
          `Chrome debugger command timed out: ${method}`,
        );
        error.commandTimedOut = true;
        return error;
      },
    );
    if (lease?.invalidated === undefined) {
      return command;
    }
    try {
      return await Promise.race([command, lease.invalidated]);
    } catch (error) {
      if (timedOut && error instanceof ProtocolError) {
        error.commandTimedOut = true;
      }
      throw error;
    }
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
    const token = this.nextStateToken(tabId);
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
      this.clearDetachedState(tabId, token, "command_timeout");
    } catch {
      if (timedOut) {
        this.quarantineTab(tabId, token, "command_timeout");
        this.trackLateDetach(tabId, token, detachPromise);
      } else if (await this.isDebuggerStillAttached(tabId)) {
        this.quarantineTab(tabId, token, "command_timeout_detach_failed");
      } else {
        this.clearDetachedState(tabId, token, "command_timeout");
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
    const token = this.nextStateToken(tabId);
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
        this.clearDetachedState(tabId, token, "controllability_lost");
      } else {
        this.quarantineTab(tabId, token, "detach_failed");
      }
      return;
    }
    this.clearDetachedState(tabId, token, "controllability_lost");
  }

  nextStateToken(tabId) {
    this.stateSequence += 1;
    this.stateTokens.set(tabId, this.stateSequence);
    return this.stateSequence;
  }

  clearDetachedState(tabId, token = null, reason = "reset") {
    if (token !== null && this.attachStates.get(tabId)?.token !== token) {
      return;
    }
    this.releaseCaptures(tabId);
    this.notifyDetached(tabId, reason);
    this.attachStates.delete(tabId);
    this.forgetChildTargets(tabId);
    this.onAttachedChange();
  }

  quarantineTab(tabId, token, reason) {
    this.notifyDetached(tabId, "quarantined");
    this.attachStates.set(tabId, { status: "quarantined", token, reason });
    this.forgetChildTargets(tabId);
    this.onAttachedChange();
  }

  notifyDetached(tabId, reason) {
    // Delete before notifying so late Chrome callbacks cannot emit a duplicate.
    if (!this.attachedTabs.delete(tabId)) {
      return false;
    }
    this.sendEvent(EVENTS.DEBUGGER_DETACHED, { tabId, reason });
    return true;
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
        this.clearDetachedState(tabId, token, "attach_failed");
      } catch {
        if (timedOut) {
          this.quarantineTab(tabId, token, "late_attach_detach_timeout");
          this.trackLateDetach(tabId, token, detachPromise);
        } else if (await this.isDebuggerStillAttached(tabId)) {
          this.quarantineTab(tabId, token, "late_attach_detach_failed");
        } else {
          this.clearDetachedState(tabId, token, "attach_failed");
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
            this.clearDetachedState(tabId, token, "quarantined");
          }),
        () =>
          this.tabScope.runTabOperation(tabId, async () => {
            if (this.attachStates.get(tabId)?.token !== token) {
              return;
            }
            if (await this.isDebuggerStillAttached(tabId)) {
              this.quarantineTab(tabId, token, "detach_failed");
            } else {
              this.clearDetachedState(tabId, token, "quarantined");
            }
          }),
      )
      .catch(() => undefined);
  }

  async ensureStillControllableLocked(tabId, lease = null) {
    try {
      await this.tabScope.assertControllableLocked(tabId, lease);
    } catch (error) {
      // COMMAND_TIMEOUT identifies lease cancellation from cancelForTabUpdate; the tab may still be controllable.
      if (
        error instanceof ProtocolError &&
        error.code === ERROR_CODES.COMMAND_TIMEOUT
      ) {
        throw error;
      }
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

  cancelRecovery(tabId) {
    const recovery = this.recoveries.get(tabId);
    if (!recovery) return;
    recovery.cancelled = true;
    recovery.lease.cancel(
      new ProtocolError(
        ERROR_CODES.DEBUGGER_DETACHED,
        "The debugger recovery was cancelled.",
      ),
    );
  }

  beginReleaseAll() {
    const releases = [
      ...new Set([
        ...(this.tabScope.scopedTabIds ?? []),
        ...this.recoveries.keys(),
        ...this.attachStates.keys(),
      ]),
    ].map((tabId) => {
      const token = this.tabScope.beginRelease?.(tabId);
      this.cancelRecovery(tabId);
      return [tabId, token];
    });
    return () => {
      for (const [tabId, token] of releases)
        this.tabScope.endRelease?.(tabId, token);
    };
  }

  handleTransportLoss() {
    const finishPrevious = this.finishTransportRelease;
    this.finishTransportRelease = this.beginReleaseAll();
    finishPrevious?.();
  }

  handleTransportConnected() {
    this.finishTransportRelease?.();
    this.finishTransportRelease = null;
  }

  ownsRecovery(recovery) {
    return (
      this.recoveries.get(recovery.tabId)?.id === recovery.id &&
      this.tabScope.scopeAdmission(recovery.tabId) === recovery.admission &&
      this.stateTokens.get(recovery.tabId) === recovery.token
    );
  }

  assertRecoveryCurrent(recovery) {
    recovery.lease.assertCurrent();
    if (
      recovery.cancelled ||
      !this.ownsRecovery(recovery) ||
      this.tabScope.isReleasePending(recovery.tabId) ||
      !this.isTransportConnected() ||
      this.getTransportGeneration() !== recovery.transportGeneration ||
      this.tabScope.operationGeneration !== recovery.operationGeneration
    ) {
      recovery.cancelled = true;
      throw new ProtocolError(
        ERROR_CODES.DEBUGGER_DETACHED,
        "The debugger recovery was cancelled.",
      );
    }
    if (
      this.detachSequences.get(recovery.tabId) !== recovery.detachSequence ||
      Date.now() >= recovery.deadline
    )
      throw new ProtocolError(
        ERROR_CODES.DEBUGGER_DETACHED,
        "The debugger recovery did not complete.",
      );
  }

  async awaitRecovery(recovery, operation, timeoutMs = Infinity) {
    this.assertRecoveryCurrent(recovery);
    try {
      return await Promise.race([
        this.withTimeout(
          () => {
            this.assertRecoveryCurrent(recovery);
            return operation();
          },
          Math.min(timeoutMs, recovery.deadline - Date.now()),
          () => {
            const error = new ProtocolError(
              ERROR_CODES.DEBUGGER_DETACHED,
              "The debugger recovery timed out.",
            );
            recovery.lease.cancel(error);
            return error;
          },
        ),
        recovery.lease.invalidated,
      ]);
    } finally {
      this.assertRecoveryCurrent(recovery);
    }
  }

  isForeignExtensionAttachError(error) {
    const urls =
      String(error?.message).match(/chrome-extension:\/\/[^\s"'<>]+/g) ?? [];
    const ownOrigin = chrome.runtime?.getURL?.("");
    return urls.some((url) => !ownOrigin || !url.startsWith(ownOrigin));
  }

  isRecoveryTab(tab) {
    const url = tab.pendingUrl || tab.url || "";
    return (
      tab.discarded !== true &&
      /^https?:\/\//i.test(url) &&
      !isRestrictedUrl(tab.url || "") &&
      !isRestrictedUrl(tab.pendingUrl || "")
    );
  }

  async revokeRecoveryLocked(recovery, lease) {
    lease.assertCurrent();
    if (!this.ownsRecovery(recovery)) return;
    if (this.attachedTabs.has(recovery.tabId)) {
      const detach = this.detachIfAttachedLocked(recovery.tabId);
      recovery.token = this.stateTokens.get(recovery.tabId);
      await detach;
      lease.assertCurrent();
      if (!this.ownsRecovery(recovery)) return;
    }
    await this.tabScope.handleDebuggerDetachLocked(recovery.tabId, lease);
  }

  scheduleRecoveryCleanup(recovery) {
    if (recovery.cleanupScheduled) return;
    recovery.cleanupScheduled = true;
    void this.tabScope
      .runTabOperation(
        recovery.tabId,
        async (lease) => {
          try {
            await this.revokeRecoveryLocked(recovery, lease);
          } finally {
            if (this.recoveries.get(recovery.tabId) === recovery)
              this.recoveries.delete(recovery.tabId);
          }
        },
        this.tabScope.operationGeneration,
        false,
      )
      .catch(() => undefined);
  }

  async recoverDetachedTabLocked(tabId, lease, detachSequence) {
    const recovery = {
      id: ++this.recoverySequence,
      tabId,
      lease,
      token: this.stateTokens.get(tabId),
      admission: this.tabScope.scopeAdmission(tabId),
      transportGeneration: this.getTransportGeneration(),
      operationGeneration: this.tabScope.operationGeneration,
      detachSequence,
      deadline: Date.now() + FOREIGN_FRAME_RECOVERY_LIMITS.deadlineMs,
      cancelled: false,
      cleanupScheduled: false,
    };
    this.recoveries.set(tabId, recovery);
    lease.onCancel(() => {
      recovery.cancelled = true;
    });
    try {
      const tab = await this.awaitRecovery(recovery, () =>
        chrome.tabs.get(tabId),
      );
      if (!this.isRecoveryTab(tab)) throw new Error("Tab is not recoverable.");
      const now = Date.now();
      const starts = (this.recoveryStarts.get(tabId) ?? []).filter(
        (started) => started > now - FOREIGN_FRAME_RECOVERY_LIMITS.windowMs,
      );
      this.recoveryStarts.set(tabId, starts);
      if (starts.length >= FOREIGN_FRAME_RECOVERY_LIMITS.recoveries)
        throw new Error("Recovery budget exhausted.");
      starts.push(now);
      for (
        let attempt = 0;
        attempt < FOREIGN_FRAME_RECOVERY_LIMITS.attempts;
        attempt += 1
      ) {
        await this.awaitRecovery(recovery, () =>
          this.removeForeignFrames(tabId),
        );
        const token = this.nextStateToken(tabId);
        recovery.token = token;
        this.attachStates.set(tabId, { status: "attaching", token });
        let rejected = false;
        const attachPromise = Promise.resolve()
          .then(() => {
            this.assertRecoveryCurrent(recovery);
            return chrome.debugger.attach({ tabId }, "1.3");
          })
          .catch((error) => {
            rejected = true;
            throw error;
          });
        try {
          await this.awaitRecovery(
            recovery,
            () => attachPromise,
            FOREIGN_FRAME_RECOVERY_LIMITS.attachTimeoutMs,
          );
        } catch (error) {
          if (rejected) {
            if (this.attachStates.get(tabId)?.token === token)
              this.attachStates.delete(tabId);
          } else {
            if (
              this.attachStates.get(tabId)?.token === token &&
              this.attachStates.get(tabId)?.status === "attaching"
            ) {
              this.attachStates.set(tabId, {
                status: "orphaned_attach",
                token,
                reason: "recovery_interrupted",
              });
            }
            this.trackLateAttach(tabId, token, attachPromise);
          }
          this.assertRecoveryCurrent(recovery);
          if (
            !rejected ||
            !this.isForeignExtensionAttachError(error) ||
            attempt + 1 === FOREIGN_FRAME_RECOVERY_LIMITS.attempts
          )
            throw error;
          await this.awaitRecovery(
            recovery,
            () =>
              new Promise((resolve) =>
                setTimeout(resolve, FOREIGN_FRAME_RECOVERY_LIMITS.retryDelayMs),
              ),
          );
          continue;
        }
        this.attachedTabs.add(tabId);
        this.attachStates.set(tabId, { status: "attached", token });
        this.onAttachedChange();
        const currentTab = await this.awaitRecovery(recovery, () =>
          this.tabScope.assertControllableLocked(tabId, lease),
        );
        if (
          !this.isRecoveryTab(currentTab) ||
          !this.tabScope.isScoped(tabId) ||
          this.tabScope.isQuarantined(tabId)
        ) {
          throw new Error("Tab is no longer recoverable.");
        }
        this.assertRecoveryCurrent(recovery);
        const fence = this.commandFences.get(tabId);
        if (!fence || fence.detachSequence !== detachSequence)
          throw new Error("Debugger detached again.");
        const announcedAt = this.attachArrivalSequence;
        const sent = this.sendEvent(EVENTS.SCOPE_TAB_ADDED, {
          ...this.tabScope.publicTab(currentTab, false),
          origin: "shared",
        });
        if (sent === false)
          throw new Error("The bridge could not announce the tab.");
        this.assertRecoveryCurrent(recovery);
        fence.announcedAt = announcedAt;
        return;
      }
    } catch {
      if (recovery.cancelled || !lease.isCurrent()) {
        this.scheduleRecoveryCleanup(recovery);
      } else {
        try {
          await this.revokeRecoveryLocked(recovery, lease);
        } catch {
          this.scheduleRecoveryCleanup(recovery);
        }
      }
    } finally {
      if (!recovery.cleanupScheduled && this.recoveries.get(tabId) === recovery)
        this.recoveries.delete(tabId);
    }
  }

  async handleDebuggerDetach(source, reason) {
    const tabId = source.tabId;
    if (!Number.isInteger(tabId)) return;
    const admission = this.tabScope.scopeAdmission?.(tabId);
    const detachSequence = (this.detachSequences.get(tabId) ?? 0) + 1;
    this.detachSequences.set(tabId, detachSequence);
    this.commandFences.set(tabId, { detachSequence, announcedAt: null });
    const release =
      reason === "canceled_by_user"
        ? this.tabScope.beginRelease?.(tabId)
        : null;
    if (reason === "canceled_by_user") this.cancelRecovery(tabId);
    try {
      await this.tabScope.runTabOperation(
        tabId,
        async (lease) => {
          lease.assertCurrent();
          if (this.tabScope.scopeAdmission?.(tabId) !== admission) {
            if (
              this.commandFences.get(tabId)?.detachSequence === detachSequence
            )
              this.clearCommandFence(tabId);
            return;
          }
          const priorState = this.attachStates.get(tabId);
          this.clearDetachedState(
            tabId,
            null,
            typeof reason === "string" ? reason : "unknown",
          );
          // An unresolved native attach still owns its compensation token.
          if (["attaching", "orphaned_attach"].includes(priorState?.status))
            this.attachStates.set(tabId, priorState);
          if (
            reason !== "target_closed" ||
            !this.tabScope.isScoped(tabId) ||
            this.tabScope.isQuarantined?.(tabId) ||
            [
              "quarantined",
              "attaching",
              "orphaned_attach",
              "detaching",
            ].includes(priorState?.status) ||
            this.tabScope.isReleasePending?.(tabId) ||
            !this.isTransportConnected()
          ) {
            await this.tabScope.handleDebuggerDetachLocked(tabId, lease);
            if (!this.tabScope.isScoped(tabId)) this.clearCommandFence(tabId);
            return;
          }
          await this.recoverDetachedTabLocked(tabId, lease, detachSequence);
        },
        this.tabScope.operationGeneration,
        true,
        (lease) => {
          lease.pageChangeExempt = true;
        },
      );
    } finally {
      if (release !== null) this.tabScope.endRelease?.(tabId, release);
    }
  }

  forgetChildTargets(tabId) {
    for (const [sessionId, target] of this.childTargets) {
      if (target.tabId === tabId) {
        this.childTargets.delete(sessionId);
      }
    }
  }
}
