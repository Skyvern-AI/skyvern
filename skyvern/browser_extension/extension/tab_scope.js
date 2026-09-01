import {
  ERROR_CODES,
  EVENTS,
  ProtocolError,
  isRestrictedUrl,
  requireArgs,
  requireTabId,
} from "./protocol.js";

const SCOPED_TAB_IDS_KEY = "scopedTabIds";
const SCOPED_GROUP_IDS_KEY = "scopedTabGroupIds";
const CREATED_TAB_IDS_KEY = "createdTabIds";
const SKYVERN_GROUP_TITLE = "Skyvern Controlled";
const SKYVERN_GROUP_COLOR = "purple";
const TAB_GROUP_ID_NONE = -1;
const ANY_GROUP_ID = Symbol("anyGroupId");
const TAB_OPERATION_TIMEOUT_MS = 28_000;
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

// Unparseable URLs fail closed: the change cancels the operation.
function urlChangeMatchesExpected(expectedUrl, url) {
  if (expectedUrl === null || expectedUrl === url) {
    return true;
  }
  try {
    return new URL(expectedUrl).href === new URL(url).href;
  } catch {
    return false;
  }
}

export class TabScope {
  constructor({ sendEvent, operationTimeoutMs = TAB_OPERATION_TIMEOUT_MS }) {
    this.sendEvent = sendEvent;
    this.scopedTabIds = new Set();
    this.quarantinedTabIds = new Set();
    this.scopedGroupIds = new Map();
    this.createdTabIds = new Set();
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

    chrome.tabs.onCreated.addListener((tab) => {
      void this.handleTabCreated(tab);
    });
    chrome.tabs.onRemoved.addListener((tabId) => {
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
          groupId >= 0
        ) {
          this.scopedGroupIds.set(numericTabId, groupId);
        }
      }
    }
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
        if (!Number.isInteger(groupId)) {
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
    if (!this.scopedTabIds.has(tabId)) {
      return;
    }
    if (Object.hasOwn(changeInfo, "url")) {
      const restricted = isRestrictedUrl(changeInfo.url);
      this.cancelTabOperations(
        tabId,
        new ProtocolError(
          restricted ? ERROR_CODES.RESTRICTED_URL : ERROR_CODES.COMMAND_TIMEOUT,
          restricted
            ? "Chrome does not allow controlling this URL."
            : "The page changed while the extension operation was running.",
        ),
        restricted
          ? null
          : (lease) => !lease.consumeUrlChangeGrant(changeInfo.url),
      );
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
    const expectedGroupId = this.scopedGroupIds.get(tabId);
    let controlledGroup = null;
    if (expectedGroupId !== undefined && tab.groupId === expectedGroupId) {
      controlledGroup = await this.getControlledGroup(tab.groupId);
      lease?.assertCurrent();
    }
    if (
      expectedGroupId === undefined ||
      tab.groupId !== expectedGroupId ||
      controlledGroup === null
    ) {
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
  }

  async unshareTab(tabId) {
    await this.ready;
    const validTabId = requireTabId(tabId);
    return this.runTabOperation(validTabId, async (lease) => {
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
    return this.runTabOperation(Symbol("tabs.create"), async (lease) => {
      let tab;
      try {
        tab = await chrome.tabs.create({ url });
      } catch {
        throw new ProtocolError(
          ERROR_CODES.INTERNAL,
          "Chrome could not create the tab.",
        );
      }
      if (!lease.isCurrent()) {
        if (Number.isInteger(tab.id)) {
          void chrome.tabs.remove(tab.id).catch(() => undefined);
        }
        lease.assertCurrent();
      }
      if (!Number.isInteger(tab.id)) {
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "Chrome did not return a tab identifier.",
        );
      }
      this.trackTabOperationLease(tab.id, lease);
      lease.assertCurrent();
      this.createdTabIds.add(tab.id);
      await this.persistScope(lease);
      try {
        const scopedTab = await this.addToScopeLocked(tab, lease);
        lease.assertCurrent();
        this.sendEvent(EVENTS.SCOPE_TAB_ADDED, {
          ...this.publicTab(scopedTab, false),
          origin: "created",
        });
        return { tabId: tab.id };
      } catch (error) {
        try {
          await this.closeCreatedTab(tab.id);
        } catch {
          // Preserve the original setup error. The tab-removal event retries persistence.
        }
        throw error;
      }
    });
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
        this.createdTabIds.add(tab.id);
        await this.persistScope(lease);
        try {
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
          if (this.scopedTabIds.has(tab.id)) {
            try {
              await this.removeFromScopeLocked(tab.id, "unshared", true, lease);
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
  }

  async handleTabRemoved(tabId) {
    await this.ready;
    await this.runTabOperation(
      tabId,
      async (lease) => {
        lease.assertCurrent();
        this.expectedGroupTransitions.delete(tabId);
        const ownershipChanged = this.createdTabIds.delete(tabId);
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

  async handleTabUpdated(tabId, changeInfo, expectedGroupTransition = null) {
    await this.ready;
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

          const controlledGroup = await this.getControlledGroup(tab.groupId);
          lease.assertCurrent();
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
              await this.removeFromScopeLocked(tabId, "unshared", true, lease);
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
      this.createdTabIds.delete(tabId);
      await this.persistScope();
    }
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
    if (this.quarantinedTabIds.has(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The requested tab is still being reconciled after reset.",
      );
    }
    this.scopedTabIds.add(tab.id);
    try {
      await this.persistScope(lease);
      lease?.assertCurrent();
      await this.groupTabLocked(tab, lease);
      lease?.assertCurrent();
      return await this.assertControllableLocked(tab.id, lease);
    } catch (error) {
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
    if (this.quarantinedTabIds.has(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.COMMAND_TIMEOUT,
        "The requested tab is still being reconciled after reset.",
      );
    }
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
    if (!Number.isInteger(tabId) || !Number.isInteger(tab.windowId)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "Chrome returned an invalid tab for Skyvern Controlled.",
      );
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
          : await chrome.tabs.group({ tabIds: [tabId] });
        grouped = true;
        lease?.assertCurrent();
      } finally {
        if (!grouped) {
          this.clearExpectedGroupTransition(tabId, transition);
        }
      }
    } catch (error) {
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
    lease?.assertCurrent();
    this.scopedGroupIds.set(tabId, groupId);
    await this.persistScope(lease);
    if (!(await this.updateControlledGroup(groupId))) {
      await this.ungroupTabLocked(tabId, groupId);
      this.scopedGroupIds.delete(tabId);
      await this.persistScope(lease);
      throw new ProtocolError(
        ERROR_CODES.INTERNAL,
        "Chrome could not label the Skyvern Controlled group.",
      );
    }
  }

  async ungroupTabLocked(tabId, scopedGroupId) {
    if (!Number.isInteger(scopedGroupId)) {
      return;
    }
    let tab;
    try {
      tab = await chrome.tabs.get(tabId);
    } catch {
      return;
    }
    if (tab.groupId !== scopedGroupId) {
      return;
    }
    const transition = this.expectGroupTransition(tabId, TAB_GROUP_ID_NONE);
    let ungrouped = false;
    try {
      await chrome.tabs.ungroup([tabId]);
      ungrouped = true;
    } catch {
      return;
    } finally {
      if (!ungrouped) {
        this.clearExpectedGroupTransition(tabId, transition);
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
      await this.runTabOperation(
        tabId,
        async (lease) => {
          let tab;
          try {
            tab = await chrome.tabs.get(tabId);
          } catch {
            lease.assertCurrent();
            await this.removeFromScopeLocked(tabId, "closed", false, lease);
            return;
          }
          lease.assertCurrent();
          if (isTabRestricted(tab)) {
            await this.removeFromScopeLocked(tabId, "unshared", true, lease);
            return;
          }
          const expectedGroupId = this.scopedGroupIds.get(tabId);
          if (
            expectedGroupId === undefined ||
            tab.groupId !== expectedGroupId
          ) {
            await this.removeFromScopeLocked(tabId, "unshared", true, lease);
            return;
          }
          await this.assertControllableLocked(tabId, lease);
        },
        generation,
      );
    }
  }

  async runTabOperation(
    tabId,
    operation,
    expectedGeneration = this.operationGeneration,
    cancelOnTabEvent = true,
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
    const invalidated = new Promise((_, reject) => {
      rejectInvalidated = reject;
    });
    void invalidated.catch(() => undefined);
    return {
      invalidated,
      isCurrent: () => !cancelled && generation === this.operationGeneration,
      remainingMs: () => Math.max(0, deadlineMs - Date.now()),
      // One grant per commanded navigation: consumed by the first URL-change
      // decision, revoked when the command fails, never carried past either.
      allowUrlChange: (expectedUrl = null) => {
        pendingUrlChangeGrant = { expectedUrl };
      },
      revokeUrlChange: () => {
        pendingUrlChangeGrant = null;
      },
      consumeUrlChangeGrant: (url) => {
        const grant = pendingUrlChangeGrant;
        pendingUrlChangeGrant = null;
        return (
          grant !== null && urlChangeMatchesExpected(grant.expectedUrl, url)
        );
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
