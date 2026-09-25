import {
  ERROR_CODES,
  EVENTS,
  ProtocolError,
  PAGE_CHANGED_WHILE_RUNNING_MESSAGE,
  PAGE_CHANGED_BEFORE_START_MESSAGE,
  isRestrictedUrl,
  requireArgs,
  requireTabId,
} from "./protocol.js";

const SCOPED_TAB_IDS_KEY = "scopedTabIds";
const SCOPED_GROUP_IDS_KEY = "scopedTabGroupIds";
const CREATED_TAB_IDS_KEY = "createdTabIds";
const PENDING_CREATION_KEY = "pendingTabCreation";
const SKYVERN_GROUP_TITLE = "Skyvern Controlled";
const SKYVERN_GROUP_COLOR = "purple";
const TAB_GROUP_ID_NONE = -1;
const ALL_WINDOW_TYPES = ["normal", "popup", "panel", "app", "devtools"];
const POPUP_GROUP_SWEEP_ALARM = "skyvern-popup-group-sweep";
const ANY_GROUP_ID = Symbol("anyGroupId");
const TAB_OPERATION_TIMEOUT_MS = 28_000;
const CREATION_TIMEOUT_MS = 3_000;
const CREATION_QUEUE_KEY = Symbol("tabs.create");

function creationError() {
  return new ProtocolError(
    ERROR_CODES.COMMAND_TIMEOUT,
    "The created tab did not settle on the requested URL.",
  );
}

function sameCreationUrl(expected, actual) {
  if (expected === "about:blank") return actual === expected;
  try {
    return new URL(expected).href === new URL(actual).href;
  } catch {
    return false;
  }
}
const SCOPE_REVOCATION_CODES = new Set([
  ERROR_CODES.TAB_NOT_FOUND,
  ERROR_CODES.TAB_NOT_SCOPED,
  ERROR_CODES.RESTRICTED_URL,
]);

function tabUrl(tab) {
  return tab.pendingUrl || tab.url || "";
}

function isTabRestricted(tab) {
  return (
    isRestrictedUrl(tab.pendingUrl || "") || isRestrictedUrl(tab.url || "")
  );
}

function isScopeRevocation(error) {
  return (
    error instanceof ProtocolError && SCOPE_REVOCATION_CODES.has(error.code)
  );
}

export class TabScope {
  constructor({ sendEvent, operationTimeoutMs = TAB_OPERATION_TIMEOUT_MS }) {
    this.sendEvent = sendEvent;
    this.scopedTabIds = new Set();
    this.quarantinedTabIds = new Set();
    this.scopedGroupIds = new Map();
    this.createdTabIds = new Set();
    this.activeCreation = null;
    this.unresolvedCreations = new Set();
    this.recoveredCreation = null;
    this.creationMarkerWrite = Promise.resolve();
    this.creationCleanups = new Map();
    this.expectedGroupTransitions = new Map();
    this.tabOperations = new Map();
    this.debuggerRouter = null;
    this.tabOperationLeases = new Map();
    this.activeOperationCount = 0;
    this.operationGeneration = 0;
    this.operationLeases = new Set();
    this.operationTimeoutMs = operationTimeoutMs;
    this.operationsIdle = Promise.resolve();
    this.resolveOperationsIdle = null;
    this.resetting = false;
    this.resetFinished = Promise.resolve();
    this.resolveResetFinished = null;
    this.ready = new Promise((resolve) => {
      this.resolveReady = resolve;
    });

    chrome.alarms.onAlarm.addListener((alarm) => {
      if (alarm.name === POPUP_GROUP_SWEEP_ALARM) {
        void this.ready.then(() => this.sweepPopupGroups());
      }
    });
    chrome.tabs.onAttached.addListener((tabId) => {
      if (this.scopedTabIds.has(tabId)) {
        this.cancelTabOperations(
          tabId,
          new ProtocolError(
            ERROR_CODES.TAB_NOT_SCOPED,
            "The controlled tab moved windows.",
          ),
        );
      }
      void this.handleTabAttached(tabId);
    });
    chrome.tabs.onCreated.addListener((tab) => {
      void this.handleTabCreated(tab);
    });
    chrome.tabs.onRemoved.addListener((tabId) => {
      this.observeCreation({ tabId, removed: true });
      this.cancelTabOperations(
        tabId,
        new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The controlled tab was closed.",
        ),
      );
      void this.handleTabRemoved(tabId);
    });
    chrome.tabs.onUpdated.addListener((tabId, changeInfo) => {
      const transition = Object.hasOwn(changeInfo, "groupId")
        ? this.getExpectedGroupTransition(tabId, changeInfo.groupId)
        : null;
      this.observeCreation({ tabId, changeInfo, transition });
      if (
        Object.hasOwn(changeInfo, "url") ||
        Object.hasOwn(changeInfo, "groupId")
      ) {
        const expectedGroupTransition = Object.hasOwn(changeInfo, "groupId")
          ? this.getExpectedGroupTransition(tabId, changeInfo.groupId)
          : null;
        this.cancelForTabUpdate(tabId, changeInfo, expectedGroupTransition);
        void this.handleTabUpdated(tabId, changeInfo, expectedGroupTransition);
      }
    });
  }

  setDebuggerRouter(debuggerRouter) {
    this.debuggerRouter = debuggerRouter;
  }

  async initialize() {
    const stored = await chrome.storage.session.get({
      [SCOPED_TAB_IDS_KEY]: [],
      [SCOPED_GROUP_IDS_KEY]: {},
      [CREATED_TAB_IDS_KEY]: [],
      [PENDING_CREATION_KEY]: null,
    });
    const storedIds = Array.isArray(stored[SCOPED_TAB_IDS_KEY])
      ? stored[SCOPED_TAB_IDS_KEY]
      : [];
    const storedCreatedIds = Array.isArray(stored[CREATED_TAB_IDS_KEY])
      ? stored[CREATED_TAB_IDS_KEY]
      : [];
    const storedGroups = stored[SCOPED_GROUP_IDS_KEY];
    for (const tabId of storedIds) {
      if (Number.isInteger(tabId) && tabId >= 0) {
        this.scopedTabIds.add(tabId);
      }
    }
    for (const tabId of storedCreatedIds) {
      if (Number.isInteger(tabId) && tabId >= 0) {
        this.createdTabIds.add(tabId);
        if (!this.scopedTabIds.has(tabId)) this.quarantinedTabIds.add(tabId);
      }
    }
    if (
      storedGroups !== null &&
      typeof storedGroups === "object" &&
      !Array.isArray(storedGroups)
    ) {
      for (const [tabId, groupId] of Object.entries(storedGroups)) {
        const numericTabId = Number(tabId);
        if (
          this.scopedTabIds.has(numericTabId) &&
          Number.isInteger(groupId) &&
          groupId >= TAB_GROUP_ID_NONE
        ) {
          this.scopedGroupIds.set(numericTabId, groupId);
        }
      }
    }
    const pending = stored[PENDING_CREATION_KEY];
    if (pending !== null && typeof pending === "object") {
      if (
        pending.tabId === null &&
        typeof pending.url === "string" &&
        Number.isFinite(pending.deadlineMs) &&
        pending.deadlineMs > Date.now()
      ) {
        // Chrome may have created the tab without returning its id. Until the
        // original deadline, no new admission can safely identify that tab.
        let finish;
        const recovered = {
          ...pending,
          finished: new Promise((resolve) => {
            finish = resolve;
          }),
          finish: () => finish(),
        };
        this.recoveredCreation = recovered;
        recovered.timer = setTimeout(
          () => this.expireRecoveredCreation(recovered),
          pending.deadlineMs - Date.now(),
        );
      } else {
        // Identified creations already have durable ownership; D13 quarantines
        // them above. Never reacquire ownership after an operator hand-back.
        await this.writeCreationMarker(null);
      }
    }
    await this.sweepPopupGroups();
    this.resolveReady();
    await this.reconcileStoredTabs();
  }

  async prepareForReset() {
    await this.ready;
    if (this.resetting) {
      await this.resetFinished;
      return this.prepareForReset();
    }
    this.resetting = true;
    this.resetFinished = new Promise((resolve) => {
      this.resolveResetFinished = resolve;
    });
    this.operationGeneration += 1;
    for (const lease of this.operationLeases) {
      lease.cancel(
        new ProtocolError(
          ERROR_CODES.COMMAND_TIMEOUT,
          "The extension operation was cancelled by reset.",
        ),
      );
    }
    await this.operationsIdle;
    await Promise.allSettled(this.creationCleanups.values());
  }

  finishReset() {
    this.resetting = false;
    this.resolveResetFinished?.();
    this.resolveResetFinished = null;
  }

  async reset() {
    const scopedGroups = [...this.scopedGroupIds];
    this.scopedTabIds.clear();
    this.scopedGroupIds.clear();
    this.expectedGroupTransitions.clear();
    this.tabOperations.clear();
    await chrome.storage.session.remove([
      SCOPED_TAB_IDS_KEY,
      SCOPED_GROUP_IDS_KEY,
    ]);
    let failedTabCount = 0;
    for (const tabId of [...this.createdTabIds]) {
      if (!(await this.closeCreatedTab(tabId))) {
        failedTabCount += 1;
      }
    }
    await Promise.all(
      scopedGroups.map(async ([tabId, groupId]) => {
        if (!Number.isInteger(groupId) || groupId < 0) {
          return;
        }
        try {
          const tab = await chrome.tabs.get(tabId);
          if (tab.groupId === groupId) {
            await chrome.tabs.ungroup([tabId]);
          }
        } catch {
          return;
        }
      }),
    );
    this.expectedGroupTransitions.clear();
    return { failedTabCount };
  }

  isScoped(tabId) {
    return this.scopedTabIds.has(tabId);
  }

  isQuarantined(tabId) {
    return this.quarantinedTabIds.has(tabId);
  }

  cancelTabOperations(tabId, error, shouldCancel = null) {
    for (const lease of this.tabOperationLeases.get(tabId) ?? []) {
      if (shouldCancel === null || shouldCancel(lease)) {
        lease.cancel(error);
      }
    }
  }
  trackTabOperationLease(tabId, lease) {
    const tabLeases = this.tabOperationLeases.get(tabId) ?? new Set();
    tabLeases.add(lease);
    this.tabOperationLeases.set(tabId, tabLeases);
  }

  untrackTabOperationLease(lease) {
    for (const [tabId, tabLeases] of this.tabOperationLeases) {
      tabLeases.delete(lease);
      if (tabLeases.size === 0) {
        this.tabOperationLeases.delete(tabId);
      }
    }
  }

  cancelForTabUpdate(tabId, changeInfo, expectedGroupTransition) {
    if (!this.scopedTabIds.has(tabId)) return;
    if (Object.hasOwn(changeInfo, "url")) {
      const restricted = isRestrictedUrl(changeInfo.url);
      for (const lease of this.tabOperationLeases.get(tabId) ?? []) {
        if (!restricted && lease.pageChangeExempt) continue;
        if (restricted || !lease.consumeUrlChangeGrant(changeInfo.url)) {
          lease.cancel(
            new ProtocolError(
              restricted
                ? ERROR_CODES.RESTRICTED_URL
                : ERROR_CODES.COMMAND_TIMEOUT,
              restricted
                ? "Chrome does not allow controlling this URL."
                : lease.debuggerCommand && !lease.dispatched
                  ? PAGE_CHANGED_BEFORE_START_MESSAGE
                  : PAGE_CHANGED_WHILE_RUNNING_MESSAGE,
            ),
          );
        }
      }
    }
    if (
      Object.hasOwn(changeInfo, "groupId") &&
      expectedGroupTransition === null &&
      changeInfo.groupId !== this.scopedGroupIds.get(tabId)
    ) {
      this.cancelTabOperations(
        tabId,
        new ProtocolError(
          ERROR_CODES.TAB_NOT_SCOPED,
          "The tab left Skyvern Controlled.",
        ),
      );
    }
  }
  async assertScoped(tabId) {
    await this.ready;
    if (!this.scopedTabIds.has(tabId)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_SCOPED,
        "The requested tab is not shared.",
      );
    }
  }

  async assertControllableLocked(tabId, lease = null) {
    await this.assertScoped(tabId);
    lease?.assertCurrent();
    let tab;
    try {
      tab = await this.getTab(tabId);
    } catch (error) {
      if (lease?.isCurrent() !== false && this.scopedTabIds.has(tabId)) {
        await this.removeFromScopeLocked(tabId, "closed", true, lease);
      }
      throw error;
    }
    lease?.assertCurrent();
    await this.assertScoped(tabId);
    let windowType;
    try {
      windowType = await this.getWindowType(tab.windowId);
    } catch (error) {
      await this.removeFromScopeLocked(tabId, "unshared", true, lease);
      throw error;
    }
    lease?.assertCurrent();
    const expectedGroupId = this.scopedGroupIds.get(tabId);
    const controlledGroup = await this.getControlledGroup(tab.groupId);
    lease?.assertCurrent();
    const validGroupless =
      expectedGroupId === TAB_GROUP_ID_NONE &&
      tab.groupId === TAB_GROUP_ID_NONE &&
      windowType !== "normal";
    const validGrouped =
      expectedGroupId !== undefined &&
      expectedGroupId >= 0 &&
      tab.groupId === expectedGroupId &&
      controlledGroup !== null &&
      windowType === "normal";
    if (!validGroupless && !validGrouped) {
      if (windowType !== "normal" && controlledGroup !== null) {
        if (!(await this.ungroupTabLocked(tabId, tab.groupId))) {
          await this.schedulePopupGroupSweep();
        }
      }
      await this.removeFromScopeLocked(tabId, "unshared", true, lease);
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_SCOPED,
        "The requested tab is no longer in Skyvern Controlled.",
      );
    }
    if (isTabRestricted(tab)) {
      await this.removeFromScopeLocked(tabId, "unshared", true, lease);
      throw new ProtocolError(
        ERROR_CODES.RESTRICTED_URL,
        "Chrome does not allow controlling this URL.",
      );
    }
    return tab;
  }

  async shareTab(tabId) {
    await this.ready;
    const validTabId = requireTabId(tabId);
    return this.runCreationAdmission(validTabId, () => {
      this.assertCreationAdmission(validTabId);
      return this.runTabOperation(validTabId, async (lease) => {
        if (this.quarantinedTabIds.has(validTabId)) {
          throw new ProtocolError(
            ERROR_CODES.COMMAND_TIMEOUT,
            "The requested tab is still being reconciled after reset.",
          );
        }
        const tab = await this.getTab(validTabId);
        lease.assertCurrent();
        if (isTabRestricted(tab)) {
          throw new ProtocolError(
            ERROR_CODES.RESTRICTED_URL,
            "Chrome does not allow sharing this URL.",
          );
        }
        if (this.scopedTabIds.has(validTabId)) {
          return {};
        }
        const scopedTab = await this.addToScopeLocked(tab, lease);
        lease.assertCurrent();
        this.sendEvent(EVENTS.SCOPE_TAB_ADDED, {
          ...this.publicTab(scopedTab, false),
          origin: "shared",
        });
        return {};
      });
    });
  }

  async unshareTab(tabId) {
    await this.ready;
    const validTabId = requireTabId(tabId);
    return this.runTabOperation(validTabId, async (lease) => {
      if (this.activeCreation?.tabId === validTabId) {
        this.activeCreation.lease.cancel(creationError());
      }
      await this.assertScoped(validTabId);
      lease.assertCurrent();
      await this.removeFromScopeLocked(validTabId, "unshared", true, lease);
      return {};
    });
  }

  async create(args) {
    const values = requireArgs(args);
    const url =
      values.url === undefined || values.url === ""
        ? "about:blank"
        : values.url;
    if (typeof url !== "string" || isRestrictedUrl(url)) {
      throw new ProtocolError(
        ERROR_CODES.RESTRICTED_URL,
        "Chrome does not allow creating this URL.",
      );
    }
    let creation;
    try {
      return await this.runTabOperation(CREATION_QUEUE_KEY, async (lease) => {
        await this.ready;
        await this.recoveredCreation?.finished;
        lease.assertCurrent();
        creation = this.beginCreation(url, lease);
        let tab;
        try {
          await this.writeCreationMarker(creation);
          lease.assertCurrent();
          try {
            tab = await chrome.tabs.create({ url });
          } catch {
            throw new ProtocolError(
              ERROR_CODES.INTERNAL,
              "Chrome could not create the tab.",
            );
          }
        } finally {
          this.unresolvedCreations.delete(creation);
        }
        if (!Number.isInteger(tab.id)) {
          throw new ProtocolError(
            ERROR_CODES.TAB_NOT_FOUND,
            "Chrome did not return a tab identifier.",
          );
        }
        creation.tabId = tab.id;
        // A cancelled, unidentified request owns only its late result, never other tabs.
        if (!lease.isCurrent()) {
          await this.cleanupCreation(creation);
          lease.assertCurrent();
        }
        if (
          this.scopedTabIds.has(tab.id) ||
          creation.scopedElsewhere.has(tab.id)
        )
          throw creationError();
        this.createdTabIds.add(tab.id);
        this.quarantinedTabIds.add(tab.id);
        this.trackTabOperationLease(tab.id, lease);
        creation.identify();
        for (const event of creation.events)
          this.applyCreationEvent(creation, event);
        creation.events.length = 0;
        lease.assertCurrent();
        await this.persistScope(lease);
        await this.writeCreationMarker(creation);
        lease.assertCurrent();
        await Promise.race([creation.complete, lease.invalidated]);
        let currentTab = await this.getCreationTab(tab.id);
        this.validateCreation(creation, currentTab);
        await this.groupTabLocked(currentTab, lease);
        await this.persistScope(lease);
        const controlledGroup = await this.getControlledGroup(
          this.scopedGroupIds.get(tab.id),
        );
        const groupedTab = await this.getCreationTab(tab.id);
        const windowType = await this.getWindowType(groupedTab.windowId);
        currentTab = await this.getCreationTab(tab.id);
        this.validateCreation(creation, currentTab);
        if (
          controlledGroup === null ||
          currentTab.groupId !== this.scopedGroupIds.get(tab.id) ||
          currentTab.windowId !== groupedTab.windowId ||
          windowType !== "normal"
        )
          throw creationError();
        this.expectedGroupTransitions.delete(tab.id);
        // One synchronous publication point. No failing work follows this commit.
        this.recordCreationOwnershipTransfer(tab.id);
        this.scopedTabIds.add(tab.id);
        this.quarantinedTabIds.delete(tab.id);
        creation.committed = true;
        try {
          this.sendEvent(EVENTS.SCOPE_TAB_ADDED, {
            ...this.publicTab(currentTab, false),
            origin: "created",
          });
        } catch {
          // The tab has committed; a transport failure cannot undo its ownership.
        } finally {
          this.endCreation(creation);
        }
        // Keep the durable pre-commit state quarantined if this write fails.
        void this.persistScope().catch(() => undefined);
        return { tabId: tab.id };
      });
    } catch (error) {
      if (creation) {
        this.endCreation(creation);
        if (!(await this.cleanupCreation(creation))) {
          throw new ProtocolError(
            ERROR_CODES.INTERNAL,
            "The created tab could not be closed.",
          );
        }
      }
      throw error;
    }
  }

  beginCreation(url, lease) {
    let identify;
    let complete;
    const creation = {
      url,
      lease,
      tabId: null,
      deadlineMs:
        Date.now() + Math.min(CREATION_TIMEOUT_MS, lease.remainingMs()),
      scopedElsewhere: new Set(this.scopedTabIds),
      events: [],
      committed: false,
      identified: new Promise((resolve) => {
        identify = resolve;
      }),
      complete: new Promise((resolve) => {
        complete = resolve;
      }),
      identify: () => identify(),
      markComplete: () => complete(),
    };
    this.activeCreation = creation;
    this.unresolvedCreations.add(creation);
    creation.timer = setTimeout(
      () => lease.cancel(creationError()),
      Math.max(0, creation.deadlineMs - Date.now()),
    );
    lease.onCancel(() => {
      this.endCreation(creation);
      // Register cleanup before reset can observe idle operations.
      void this.cleanupCreation(creation).catch(() => undefined);
    });
    return creation;
  }

  endCreation(creation) {
    clearTimeout(creation.timer);
    creation.events.length = 0;
    creation.identify();
    if (this.activeCreation === creation) {
      this.activeCreation = null;
      void this.writeCreationMarker(null).catch(() => undefined);
    }
  }

  writeCreationMarker(creation) {
    const marker =
      creation === null
        ? null
        : {
            url: creation.url,
            deadlineMs: creation.deadlineMs,
            tabId: creation.tabId,
          };
    // Serialize marker writes so a late write cannot resurrect a failed creation
    // or erase the marker for the next request in the creation queue.
    this.creationMarkerWrite = this.creationMarkerWrite
      .catch(() => undefined)
      .then(() =>
        chrome.storage.session.set({ [PENDING_CREATION_KEY]: marker }),
      );
    return this.creationMarkerWrite;
  }

  expireRecoveredCreation(recovered) {
    if (this.recoveredCreation !== recovered) return;
    this.recoveredCreation = null;
    clearTimeout(recovered.timer);
    void this.writeCreationMarker(null).catch(() => undefined);
    recovered.finish();
  }

  hasRecoveredCreationFence(tabId) {
    const recovered = this.recoveredCreation;
    if (recovered && Date.now() >= recovered.deadlineMs) {
      this.expireRecoveredCreation(recovered);
    }
    return !this.scopedTabIds.has(tabId) && this.recoveredCreation !== null;
  }

  recordCreationOwnershipTransfer(tabId) {
    for (const creation of this.unresolvedCreations) {
      creation.scopedElsewhere.add(tabId);
    }
  }

  observeCreation(event) {
    const creation = this.activeCreation;
    if (!creation) return;
    if (creation.tabId === null) creation.events.push(event);
    else this.applyCreationEvent(creation, event);
  }

  applyCreationEvent(creation, event) {
    if (event.tabId !== creation.tabId || !creation.lease.isCurrent()) return;
    if (event.removed) {
      creation.lease.cancel(
        new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The created tab closed during creation.",
        ),
      );
      return;
    }
    const change = event.changeInfo;
    if (
      (Object.hasOwn(change, "url") &&
        !sameCreationUrl(creation.url, change.url)) ||
      (Object.hasOwn(change, "groupId") && event.transition === null)
    ) {
      creation.lease.cancel(creationError());
      return;
    }
    if (change.status === "complete") creation.markComplete();
  }

  async getCreationTab(tabId) {
    try {
      return await this.getTab(tabId);
    } catch (error) {
      if (error.code === ERROR_CODES.TAB_NOT_FOUND) {
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The created tab closed during creation.",
        );
      }
      throw error;
    }
  }

  validateCreation(creation, tab) {
    creation.lease.assertCurrent();
    if (
      this.scopedTabIds.has(tab.id) ||
      tab.status !== "complete" ||
      !sameCreationUrl(creation.url, tab.url) ||
      (tab.pendingUrl && !sameCreationUrl(creation.url, tab.pendingUrl))
    ) {
      throw creationError();
    }
  }

  async waitForCreationIdentification(tabId) {
    while (
      !this.scopedTabIds.has(tabId) &&
      this.activeCreation?.tabId === null
    ) {
      await this.activeCreation.identified;
    }
  }

  async runCreationAdmission(tabId, operation, ignoreReset = false) {
    const generation = this.operationGeneration;
    while (true) {
      await this.waitForCreationIdentification(tabId);
      if (this.hasRecoveredCreationFence(tabId)) {
        if (ignoreReset) return;
        this.assertCreationAdmission(tabId);
      }
      if (generation !== this.operationGeneration) {
        if (ignoreReset) return;
        throw new ProtocolError(
          ERROR_CODES.COMMAND_TIMEOUT,
          "The extension operation was cancelled by reset.",
        );
      }
      try {
        return await operation();
      } catch (error) {
        if (!error.creationIdentification) throw error;
        // A creation began during an earlier await. Release all tab queues
        // before waiting, then retry admission against current scope.
        await error.creationIdentification;
      }
    }
  }

  assertCreationAdmission(tabId) {
    if (
      this.hasRecoveredCreationFence(tabId) ||
      (!this.scopedTabIds.has(tabId) &&
        this.activeCreation &&
        (this.activeCreation.tabId === null ||
          this.activeCreation.tabId === tabId))
    ) {
      const error = new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The requested tab is still being created.",
      );
      if (this.activeCreation?.tabId === null) {
        error.creationIdentification = this.activeCreation.identified;
      }
      throw error;
    }
  }

  cleanupCreation(creation) {
    const tabId = creation.tabId;
    if (
      tabId === null ||
      creation.committed ||
      creation.scopedElsewhere.has(tabId) ||
      this.scopedTabIds.has(tabId)
    )
      return Promise.resolve(true);
    if (creation.cleanup) return creation.cleanup;
    this.createdTabIds.add(tabId);
    this.quarantinedTabIds.add(tabId);
    this.expectedGroupTransitions.delete(tabId);
    this.scopedGroupIds.delete(tabId);
    const cleanup = (async () => {
      try {
        await this.persistScope();
      } catch {
        // A storage failure must not skip physical cleanup.
      }
      if (this.scopedTabIds.has(tabId)) return true;
      return this.closeCreatedTab(tabId);
    })().finally(() => this.creationCleanups.delete(tabId));
    creation.cleanup = cleanup;
    this.creationCleanups.set(tabId, cleanup);
    return cleanup;
  }

  async remove(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.runTabOperation(tabId, async (lease) => {
      const created = this.createdTabIds.has(tabId);
      if (this.scopedTabIds.has(tabId)) {
        await this.assertControllableLocked(tabId, lease);
      } else if (!created) {
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_SCOPED,
          "The requested tab is not shared.",
        );
      }
      if (this.quarantinedTabIds.has(tabId)) {
        throw new ProtocolError(
          ERROR_CODES.COMMAND_TIMEOUT,
          "The requested tab is still being reconciled after reset.",
        );
      }
      this.quarantinedTabIds.add(tabId);
      lease.assertCurrent();
      let removePromise;
      try {
        removePromise = chrome.tabs.remove(tabId);
      } catch (error) {
        this.quarantinedTabIds.delete(tabId);
        throw error;
      }
      const trackedRemove = Promise.resolve(removePromise).finally(() => {
        this.quarantinedTabIds.delete(tabId);
      });
      void trackedRemove.catch(() => undefined);
      try {
        await trackedRemove;
      } catch {
        if (!this.scopedTabIds.has(tabId)) {
          if (created) {
            try {
              await chrome.tabs.get(tabId);
            } catch {
              this.createdTabIds.delete(tabId);
              await this.persistScope();
              return {};
            }
            throw new ProtocolError(
              ERROR_CODES.INTERNAL,
              "Chrome could not close the created tab.",
            );
          }
          return {};
        }
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The requested tab was not found.",
        );
      }
      lease.assertCurrent();
      this.createdTabIds.delete(tabId);
      if (this.scopedTabIds.has(tabId)) {
        await this.removeFromScopeLocked(tabId, "closed", false, lease);
      } else {
        await this.persistScope(lease);
      }
      return {};
    });
  }

  async activate(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.runTabOperation(tabId, async (lease) => {
      const tab = await this.assertControllableLocked(tabId, lease);
      try {
        await chrome.tabs.update(tabId, { active: true });
        if (Number.isInteger(tab.windowId)) {
          await chrome.windows.update(tab.windowId, { focused: true });
        }
      } catch {
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The requested tab was not found.",
        );
      }
      lease.assertCurrent();
      await this.assertControllableLocked(tabId, lease);
      return {};
    });
  }

  async list() {
    await this.ready;
    const tabs = await this.collectScopedTabs(true);
    let focusedTabId = null;
    try {
      const [focusedTab] = await chrome.tabs.query({
        active: true,
        lastFocusedWindow: true,
      });
      if (Number.isInteger(focusedTab?.id)) {
        focusedTabId = focusedTab.id;
      }
    } catch {}
    for (const tab of tabs) {
      tab.active = tab.tabId === focusedTabId;
    }
    return { tabs };
  }

  async helloTabs() {
    await this.ready;
    return this.collectScopedTabs(false);
  }

  async handleDebuggerDetachLocked(tabId, lease = null) {
    await this.ready;
    lease?.assertCurrent();
    if (this.scopedTabIds.has(tabId)) {
      await this.removeFromScopeLocked(tabId, "detached", false, lease);
    }
  }

  async handleTabCreated(tab) {
    await this.ready;
    if (!Number.isInteger(tab.id) || !Number.isInteger(tab.openerTabId)) {
      return;
    }
    const admitCreatedTab = async () => {
      if (
        this.quarantinedTabIds.has(tab.id) ||
        this.activeCreation?.tabId === tab.id
      )
        return;
      await this.runTabOperation(tab.openerTabId, async (openerLease) => {
        try {
          await this.assertControllableLocked(tab.openerTabId, openerLease);
        } catch (error) {
          if (isScopeRevocation(error)) {
            return;
          }
          throw error;
        }
        if (isTabRestricted(tab)) {
          return;
        }
        await this.runTabOperation(tab.id, async (lease) => {
          openerLease.assertCurrent();
          await this.assertControllableLocked(tab.openerTabId, openerLease);
          this.assertCreationAdmission(tab.id);
          try {
            this.createdTabIds.add(tab.id);
            await this.persistScope(lease);
            const scopedTab = await this.addToScopeLocked(tab, lease);
            openerLease.assertCurrent();
            await this.assertControllableLocked(tab.openerTabId, openerLease);
            lease.assertCurrent();
            this.sendEvent(EVENTS.TABS_CREATED, {
              tabId: scopedTab.id,
              openerTabId: tab.openerTabId,
              url: tabUrl(scopedTab),
            });
          } catch (error) {
            if (error.creationIdentification) {
              this.createdTabIds.delete(tab.id);
              await this.persistScope();
              throw error;
            }
            if (this.scopedTabIds.has(tab.id)) {
              try {
                await this.removeFromScopeLocked(
                  tab.id,
                  "unshared",
                  true,
                  lease,
                );
              } catch {
                // Continue closing the child tab.
              }
            }
            try {
              await this.closeCreatedTab(tab.id);
            } catch {
              // Preserve the original setup error. The tab-removal event retries persistence.
            }
            throw error;
          }
        });
      });
    };
    return this.runCreationAdmission(tab.id, admitCreatedTab, true);
  }

  async handleTabRemoved(tabId) {
    await this.ready;
    await this.runTabOperation(
      tabId,
      async (lease) => {
        lease.assertCurrent();
        this.expectedGroupTransitions.delete(tabId);
        const ownershipChanged = this.createdTabIds.delete(tabId);
        this.quarantinedTabIds.delete(tabId);
        if (this.scopedTabIds.has(tabId)) {
          await this.removeFromScopeLocked(tabId, "closed", false, lease);
        } else if (ownershipChanged) {
          await this.persistScope(lease);
        }
      },
      this.operationGeneration,
      false,
    );
  }

  async handleTabAttached(tabId) {
    await this.ready;
    if (!this.scopedTabIds.has(tabId)) return;
    this.cancelTabOperations(
      tabId,
      new ProtocolError(
        ERROR_CODES.TAB_NOT_SCOPED,
        "The controlled tab moved windows.",
      ),
    );
    await this.runTabOperation(
      tabId,
      (lease) => this.removeFromScopeLocked(tabId, "unshared", true, lease),
      this.operationGeneration,
      false,
    );
  }

  async handleTabUpdated(tabId, changeInfo, expectedGroupTransition = null) {
    await this.ready;
    const admitTabUpdate = async () => {
      if (
        !this.scopedTabIds.has(tabId) &&
        (this.quarantinedTabIds.has(tabId) ||
          this.activeCreation?.tabId === tabId)
      )
        return;
      await this.runTabOperation(
        tabId,
        async (lease) => {
          try {
            lease.assertCurrent();
            if (
              this.scopedTabIds.has(tabId) &&
              Object.hasOwn(changeInfo, "url") &&
              isRestrictedUrl(changeInfo.url)
            ) {
              await this.removeFromScopeLocked(tabId, "unshared", true, lease);
              return;
            }
            if (
              !Object.hasOwn(changeInfo, "groupId") ||
              expectedGroupTransition !== null
            ) {
              return;
            }

            let tab;
            try {
              tab = await chrome.tabs.get(tabId);
            } catch {
              return;
            }
            lease.assertCurrent();
            if (tab.groupId !== changeInfo.groupId) {
              return;
            }

            let windowType;
            try {
              windowType = await this.getWindowType(tab.windowId);
            } catch {
              await this.removeFromScopeLocked(tabId, "unshared", true, lease);
              return;
            }
            let controlledGroup = null;
            if (tab.groupId !== TAB_GROUP_ID_NONE) {
              try {
                const group = await chrome.tabGroups.get(tab.groupId);
                if (group.title === SKYVERN_GROUP_TITLE)
                  controlledGroup = group;
              } catch {
                if (windowType !== "normal")
                  await this.schedulePopupGroupSweep();
              }
            }
            lease.assertCurrent();
            if (windowType !== "normal" && controlledGroup !== null) {
              const ungrouped = await this.ungroupTabLocked(tabId, tab.groupId);
              lease.assertCurrent();
              try {
                await this.removeFromScopeLocked(
                  tabId,
                  "unshared",
                  true,
                  lease,
                );
              } finally {
                if (!ungrouped) await this.schedulePopupGroupSweep();
              }
              return;
            }
            if (this.scopedTabIds.has(tabId)) {
              const expectedGroupId = this.scopedGroupIds.get(tabId);
              if (tab.groupId === expectedGroupId) {
                return;
              }
              if (controlledGroup !== null) {
                this.scopedGroupIds.set(tabId, tab.groupId);
                await this.persistScope(lease);
                lease.assertCurrent();
                await this.updateControlledGroup(tab.groupId);
                return;
              }
              if (expectedGroupId !== undefined) {
                await this.removeFromScopeLocked(
                  tabId,
                  "unshared",
                  true,
                  lease,
                );
              }
              return;
            }

            if (controlledGroup === null) {
              return;
            }
            if (isTabRestricted(tab)) {
              lease.assertCurrent();
              await this.ungroupTabLocked(tabId, tab.groupId);
              return;
            }

            if (this.quarantinedTabIds.has(tabId)) {
              throw new ProtocolError(
                ERROR_CODES.COMMAND_TIMEOUT,
                "The requested tab is still being reconciled after reset.",
              );
            }
            lease.assertCurrent();
            await this.updateControlledGroup(tab.groupId);
            lease.assertCurrent();
            const scopedTab = await this.addGroupedTabToScopeLocked(
              tab,
              tab.groupId,
              lease,
            );
            lease.assertCurrent();
            this.sendEvent(EVENTS.SCOPE_TAB_ADDED, {
              ...this.publicTab(scopedTab, false),
              origin: "shared",
            });
          } finally {
            if (expectedGroupTransition !== null) {
              this.clearExpectedGroupTransition(tabId, expectedGroupTransition);
            }
          }
        },
        this.operationGeneration,
        false,
      );
    };
    return this.runCreationAdmission(tabId, admitTabUpdate, true);
  }

  async closeCreatedTab(tabId) {
    let tabExists = true;
    try {
      await chrome.tabs.remove(tabId);
      tabExists = false;
    } catch {
      try {
        await chrome.tabs.get(tabId);
      } catch {
        tabExists = false;
      }
    }
    if (!tabExists) {
      this.quarantinedTabIds.delete(tabId);
      this.createdTabIds.delete(tabId);
      await this.persistScope();
    }
    if (tabExists && this.createdTabIds.has(tabId))
      this.quarantinedTabIds.add(tabId);
    return !tabExists;
  }

  async addToScopeLocked(tab, lease = null) {
    if (!Number.isInteger(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "The requested tab was not found.",
      );
    }
    lease?.assertCurrent();
    this.assertCreationAdmission(tab.id);
    if (this.quarantinedTabIds.has(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The requested tab is still being reconciled after reset.",
      );
    }
    this.recordCreationOwnershipTransfer(tab.id);
    this.scopedTabIds.add(tab.id);
    try {
      await this.persistScope(lease);
      lease?.assertCurrent();
      await this.groupTabLocked(tab, lease);
      lease?.assertCurrent();
      return await this.assertControllableLocked(tab.id, lease);
    } catch (error) {
      const scopedGroupId = this.scopedGroupIds.get(tab.id);
      if (Number.isInteger(scopedGroupId) && scopedGroupId >= 0) {
        await this.ungroupTabLocked(tab.id, scopedGroupId);
      }
      this.scopedTabIds.delete(tab.id);
      this.scopedGroupIds.delete(tab.id);
      await this.persistScope(lease);
      throw error;
    }
  }

  async addGroupedTabToScopeLocked(tab, groupId, lease = null) {
    if (!Number.isInteger(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "The requested tab was not found.",
      );
    }
    lease?.assertCurrent();
    this.assertCreationAdmission(tab.id);
    if (this.quarantinedTabIds.has(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The requested tab is still being reconciled after reset.",
      );
    }
    this.recordCreationOwnershipTransfer(tab.id);
    this.scopedTabIds.add(tab.id);
    this.scopedGroupIds.set(tab.id, groupId);
    await this.persistScope(lease);
    lease?.assertCurrent();
    return this.assertControllableLocked(tab.id, lease);
  }

  async removeFromScopeLocked(tabId, reason, detach, lease = null) {
    lease?.assertCurrent();
    if (!this.scopedTabIds.delete(tabId)) {
      return;
    }
    // Hand the tab back to the operator. A later reset must not close it.
    this.createdTabIds.delete(tabId);
    this.expectedGroupTransitions.delete(tabId);
    const scopedGroupId = this.scopedGroupIds.get(tabId);
    this.scopedGroupIds.delete(tabId);
    try {
      if (detach && this.debuggerRouter !== null) {
        await this.debuggerRouter.detachIfAttachedLocked(tabId);
      }
      lease?.assertCurrent();
    } finally {
      try {
        await this.persistScope(lease);
        lease?.assertCurrent();
      } finally {
        lease?.assertCurrent();
        await this.ungroupTabLocked(tabId, scopedGroupId);
        lease?.assertCurrent();
        this.sendEvent(EVENTS.SCOPE_TAB_REMOVED, { tabId, reason });
      }
    }
  }

  async groupTabLocked(tab, lease = null) {
    const tabId = tab.id;
    tab = await this.getTab(tabId);
    lease?.assertCurrent();
    if (!Number.isInteger(tabId) || !Number.isInteger(tab.windowId)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "Chrome returned an invalid tab for Skyvern Controlled.",
      );
    }
    const windowType = await this.getWindowType(tab.windowId);
    lease?.assertCurrent();
    if (windowType !== "normal") {
      if (tab.groupId !== TAB_GROUP_ID_NONE) {
        const controlledGroup = await this.getControlledGroup(tab.groupId);
        lease?.assertCurrent();
        if (
          controlledGroup === null ||
          !(await this.ungroupTabLocked(tabId, tab.groupId))
        ) {
          if (controlledGroup !== null) await this.schedulePopupGroupSweep();
          throw new ProtocolError(
            ERROR_CODES.TAB_NOT_SCOPED,
            "The popup tab could not be ungrouped.",
          );
        }
      }
      lease?.assertCurrent();
      this.scopedGroupIds.set(tabId, TAB_GROUP_ID_NONE);
      await this.persistScope(lease);
      return;
    }
    let groupId;
    try {
      const groups = await chrome.tabGroups.query({ windowId: tab.windowId });
      lease?.assertCurrent();
      const existingGroup = groups.find(
        (group) => group.title === SKYVERN_GROUP_TITLE,
      );
      const expectedGroupId = existingGroup?.id ?? ANY_GROUP_ID;
      const transition = this.expectGroupTransition(tabId, expectedGroupId);
      let grouped = false;
      try {
        groupId = existingGroup
          ? await chrome.tabs.group({
              groupId: existingGroup.id,
              tabIds: [tabId],
            })
          : await chrome.tabs.group({
              tabIds: [tabId],
              // Chrome otherwise creates the group in the current window.
              createProperties: { windowId: tab.windowId },
            });
        grouped = true;
        lease?.assertCurrent();
        const groupedTab = await this.getTab(tabId);
        lease?.assertCurrent();
        if (
          groupedTab.windowId !== tab.windowId ||
          groupedTab.groupId !== groupId
        ) {
          throw new ProtocolError(
            ERROR_CODES.TAB_NOT_SCOPED,
            "The tab moved while grouping.",
          );
        }
      } finally {
        if (!grouped) {
          this.clearExpectedGroupTransition(tabId, transition);
        }
      }
      lease?.assertCurrent();
      this.scopedGroupIds.set(tabId, groupId);
      await this.persistScope(lease);
      if (!(await this.updateControlledGroup(groupId))) {
        throw new ProtocolError(
          ERROR_CODES.INTERNAL,
          "Chrome could not label the Skyvern Controlled group.",
        );
      }
      lease?.assertCurrent();
    } catch (error) {
      // A cancelled lease must still undo a completed Chrome grouping.
      if (Number.isInteger(groupId)) {
        await this.ungroupTabLocked(tabId, groupId);
      }
      lease?.assertCurrent();
      this.scopedGroupIds.delete(tabId);
      await this.persistScope(lease);
      if (error instanceof ProtocolError) {
        throw error;
      }
      throw new ProtocolError(
        ERROR_CODES.INTERNAL,
        "Chrome could not add the tab to Skyvern Controlled.",
      );
    }
  }

  async getWindowType(windowId) {
    try {
      const window = await chrome.windows.get(windowId, {
        windowTypes: ALL_WINDOW_TYPES,
      });
      if (!ALL_WINDOW_TYPES.includes(window.type)) {
        throw new Error("Unknown window type");
      }
      return window.type;
    } catch {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_SCOPED,
        "The tab's window could not be verified.",
      );
    }
  }

  async ungroupTabLocked(tabId, scopedGroupId) {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      let transition = null;
      let ungrouped = false;
      try {
        const tab = await chrome.tabs.get(tabId);
        if (tab.groupId === TAB_GROUP_ID_NONE) return true;
        if (
          !Number.isInteger(scopedGroupId) ||
          scopedGroupId < 0 ||
          tab.groupId !== scopedGroupId
        ) {
          return false;
        }
        transition = this.expectGroupTransition(tabId, TAB_GROUP_ID_NONE);
        await chrome.tabs.ungroup([tabId]);
        ungrouped =
          (await chrome.tabs.get(tabId)).groupId === TAB_GROUP_ID_NONE;
        if (ungrouped) return true;
      } catch {
        // Chrome can reject an ungroup while it restores or moves a tab.
      } finally {
        if (!ungrouped && transition !== null) {
          this.clearExpectedGroupTransition(tabId, transition);
        }
      }
    }
    return false;
  }

  async schedulePopupGroupSweep() {
    if (await chrome.alarms.get(POPUP_GROUP_SWEEP_ALARM)) return;
    await chrome.alarms.create(POPUP_GROUP_SWEEP_ALARM, {
      delayInMinutes: 0.5,
    });
  }

  async sweepPopupGroups() {
    let retry = false;
    try {
      const windows = await chrome.windows.getAll({
        populate: true,
        windowTypes: ALL_WINDOW_TYPES,
      });
      for (const window of windows) {
        if (window.type === "normal") continue;
        for (const tab of window.tabs ?? []) {
          try {
            await this.runTabOperation(tab.id, async (lease) => {
              const liveTab = await this.getTab(tab.id);
              const windowType = await this.getWindowType(liveTab.windowId);
              lease.assertCurrent();
              if (
                windowType === "normal" ||
                liveTab.groupId === TAB_GROUP_ID_NONE
              )
                return;
              const group = await chrome.tabGroups.get(liveTab.groupId);
              lease.assertCurrent();
              if (group.title !== SKYVERN_GROUP_TITLE) return;
              if (!(await this.ungroupTabLocked(tab.id, liveTab.groupId))) {
                retry = true;
              }
              lease.assertCurrent();
              await this.removeFromScopeLocked(tab.id, "unshared", true, lease);
            });
          } catch (error) {
            retry = true;
            if (isScopeRevocation(error)) {
              await this.removeFromScopeLocked(tab.id, "unshared", true);
            }
          }
        }
      }
    } catch {
      retry = true;
    }
    if (retry) {
      try {
        await this.schedulePopupGroupSweep();
      } catch {
        // Alarm failures must not prevent the service worker from becoming ready.
      }
    }
  }

  async getControlledGroup(groupId) {
    if (!Number.isInteger(groupId) || groupId === TAB_GROUP_ID_NONE) {
      return null;
    }
    try {
      const group = await chrome.tabGroups.get(groupId);
      return group.title === SKYVERN_GROUP_TITLE ? group : null;
    } catch {
      return null;
    }
  }

  async updateControlledGroup(groupId) {
    try {
      await chrome.tabGroups.update(groupId, {
        title: SKYVERN_GROUP_TITLE,
        color: SKYVERN_GROUP_COLOR,
      });
      return true;
    } catch {
      return false;
    }
  }

  expectGroupTransition(tabId, groupId) {
    const transition = { groupId };
    this.expectedGroupTransitions.set(tabId, transition);
    return transition;
  }

  getExpectedGroupTransition(tabId, groupId) {
    const transition = this.expectedGroupTransitions.get(tabId);
    if (
      transition === undefined ||
      (transition.groupId !== ANY_GROUP_ID && transition.groupId !== groupId) ||
      (transition.groupId === ANY_GROUP_ID && groupId === TAB_GROUP_ID_NONE)
    ) {
      return null;
    }
    return transition;
  }

  clearExpectedGroupTransition(tabId, transition) {
    if (this.expectedGroupTransitions.get(tabId) === transition) {
      this.expectedGroupTransitions.delete(tabId);
    }
  }

  async collectScopedTabs(includeActive) {
    const generation = this.operationGeneration;
    const tabs = [];
    for (const tabId of [...this.scopedTabIds]) {
      const tab = await this.runTabOperation(
        tabId,
        async (lease) => {
          if (!this.scopedTabIds.has(tabId)) {
            return null;
          }
          try {
            const scopedTab = await this.assertControllableLocked(tabId, lease);
            return this.publicTab(scopedTab, includeActive);
          } catch (error) {
            if (isScopeRevocation(error)) {
              return null;
            }
            throw error;
          }
        },
        generation,
      );
      if (tab !== null) {
        tabs.push(tab);
      }
    }
    return tabs;
  }

  publicTab(tab, includeActive) {
    const result = {
      tabId: tab.id,
      url: tabUrl(tab),
      title: typeof tab.title === "string" ? tab.title : "",
    };
    if (includeActive) {
      result.active = tab.active === true;
    }
    return result;
  }

  async getTab(tabId) {
    try {
      return await chrome.tabs.get(tabId);
    } catch {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "The requested tab was not found.",
      );
    }
  }

  async reconcileStoredTabs() {
    let ownershipChanged = false;
    for (const tabId of [...this.createdTabIds]) {
      try {
        await chrome.tabs.get(tabId);
      } catch {
        this.createdTabIds.delete(tabId);
        ownershipChanged = true;
      }
    }
    if (ownershipChanged) {
      await this.persistScope();
    }
    const generation = this.operationGeneration;
    for (const tabId of [...this.scopedTabIds]) {
      try {
        await this.runTabOperation(
          tabId,
          (lease) => this.assertControllableLocked(tabId, lease),
          generation,
        );
      } catch (error) {
        if (!isScopeRevocation(error)) throw error;
      }
    }
  }

  async runTabOperation(
    tabId,
    operation,
    expectedGeneration = this.operationGeneration,
    cancelOnTabEvent = true,
    onLeaseCreated = null,
  ) {
    while (this.resetting) {
      await this.resetFinished;
    }
    if (expectedGeneration !== this.operationGeneration) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The extension operation was cancelled by reset.",
      );
    }
    const deadlineMs = Date.now() + this.operationTimeoutMs;
    const lease = this.createOperationLease(deadlineMs);
    onLeaseCreated?.(lease);
    const timeoutId = setTimeout(
      () => {
        lease.cancel(
          new ProtocolError(
            ERROR_CODES.COMMAND_TIMEOUT,
            "The extension tab operation timed out.",
          ),
        );
      },
      Math.max(0, deadlineMs - Date.now()),
    );
    if (this.activeOperationCount === 0) {
      this.operationsIdle = new Promise((resolve) => {
        this.resolveOperationsIdle = resolve;
      });
    }
    this.activeOperationCount += 1;
    this.operationLeases.add(lease);
    if (cancelOnTabEvent) {
      this.trackTabOperationLease(tabId, lease);
    }
    const previous = this.tabOperations.get(tabId) ?? Promise.resolve();
    const current = previous
      .catch(() => undefined)
      .then(() => this.runOperationWithLease(lease, operation));
    this.tabOperations.set(tabId, current);
    try {
      return await current;
    } finally {
      lease.revokeUrlChange();
      if (this.tabOperations.get(tabId) === current) {
        this.tabOperations.delete(tabId);
      }
      this.operationLeases.delete(lease);
      this.untrackTabOperationLease(lease);
      clearTimeout(timeoutId);
      this.activeOperationCount -= 1;
      if (this.activeOperationCount === 0) {
        this.resolveOperationsIdle?.();
        this.resolveOperationsIdle = null;
      }
    }
  }

  createOperationLease(deadlineMs) {
    const generation = this.operationGeneration;
    let rejectInvalidated;
    let cancelled = false;
    let cancellationError = null;
    let pendingUrlChangeGrant = null;
    const cancellationCallbacks = new Set();
    const invalidated = new Promise((_, reject) => {
      rejectInvalidated = reject;
    });
    void invalidated.catch(() => undefined);
    return {
      invalidated,
      onCancel: (callback) => {
        if (cancelled) callback();
        else cancellationCallbacks.add(callback);
      },
      isCurrent: () => !cancelled && generation === this.operationGeneration,
      remainingMs: () => Math.max(0, deadlineMs - Date.now()),
      // A commanded navigation accepts every non-restricted URL event while in flight.
      // The grant is revoked when the operation ends.
      allowUrlChange: () => {
        pendingUrlChangeGrant = {
          redirected: false,
        };
      },
      hasUrlChangeGrant: () => pendingUrlChangeGrant !== null,
      revokeUrlChange: () => {
        pendingUrlChangeGrant = null;
      },
      consumeUrlChangeGrant: () => {
        const grant = pendingUrlChangeGrant;
        if (grant === null) {
          return false;
        }
        grant.redirected = true;
        return true;
      },
      assertCurrent: () => {
        if (cancelled || generation !== this.operationGeneration) {
          throw (
            cancellationError ??
            new ProtocolError(
              ERROR_CODES.COMMAND_TIMEOUT,
              "The extension operation is no longer current.",
            )
          );
        }
      },
      cancel: (error) => {
        if (cancelled) {
          return;
        }
        cancelled = true;
        cancellationError = error;
        for (const callback of cancellationCallbacks) callback();
        cancellationCallbacks.clear();
        rejectInvalidated(error);
      },
    };
  }

  async runOperationWithLease(lease, operation) {
    lease.assertCurrent();
    return Promise.race([
      Promise.resolve().then(() => operation(lease)),
      lease.invalidated,
    ]);
  }

  async persistScope(lease = null) {
    const values = {
      [SCOPED_TAB_IDS_KEY]: [...this.scopedTabIds],
      [SCOPED_GROUP_IDS_KEY]: Object.fromEntries(this.scopedGroupIds),
      [CREATED_TAB_IDS_KEY]: [...this.createdTabIds],
    };
    await chrome.storage.session.set(values);
    if (lease !== null && !lease.isCurrent()) {
      await chrome.storage.session.set({
        [SCOPED_TAB_IDS_KEY]: [...this.scopedTabIds],
        [SCOPED_GROUP_IDS_KEY]: Object.fromEntries(this.scopedGroupIds),
        [CREATED_TAB_IDS_KEY]: [...this.createdTabIds],
      });
      lease.assertCurrent();
    }
  }
}
