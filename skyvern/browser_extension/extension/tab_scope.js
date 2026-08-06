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
const SKYVERN_GROUP_TITLE = "Skyvern Controlled";
const SKYVERN_GROUP_COLOR = "purple";
const TAB_GROUP_ID_NONE = -1;
const ANY_GROUP_ID = Symbol("anyGroupId");

function tabUrl(tab) {
  return tab.pendingUrl || tab.url || "";
}

function isTabRestricted(tab) {
  return (
    isRestrictedUrl(tab.pendingUrl || "") || isRestrictedUrl(tab.url || "")
  );
}

export class TabScope {
  constructor({ sendEvent }) {
    this.sendEvent = sendEvent;
    this.scopedTabIds = new Set();
    this.scopedGroupIds = new Map();
    this.expectedGroupTransitions = new Map();
    this.tabOperations = new Map();
    this.debuggerRouter = null;
    this.ready = new Promise((resolve) => {
      this.resolveReady = resolve;
    });

    chrome.tabs.onCreated.addListener((tab) => {
      void this.handleTabCreated(tab);
    });
    chrome.tabs.onRemoved.addListener((tabId) => {
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
    });
    const storedIds = Array.isArray(stored[SCOPED_TAB_IDS_KEY])
      ? stored[SCOPED_TAB_IDS_KEY]
      : [];
    const storedGroups = stored[SCOPED_GROUP_IDS_KEY];
    for (const tabId of storedIds) {
      if (Number.isInteger(tabId) && tabId >= 0) {
        this.scopedTabIds.add(tabId);
      }
    }
    if (
      storedGroups !== null &&
      typeof storedGroups === "object" &&
      !Array.isArray(storedGroups)
    ) {
      for (const [tabId, groupId] of Object.entries(storedGroups)) {
        const numericTabId = Number(tabId);
        if (this.scopedTabIds.has(numericTabId) && Number.isInteger(groupId)) {
          this.scopedGroupIds.set(numericTabId, groupId);
        }
      }
    }
    this.resolveReady();
    await this.reconcileStoredTabs();
  }

  isScoped(tabId) {
    return this.scopedTabIds.has(tabId);
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

  async assertControllableLocked(tabId) {
    await this.assertScoped(tabId);
    let tab;
    try {
      tab = await this.getTab(tabId);
    } catch (error) {
      if (this.scopedTabIds.has(tabId)) {
        await this.removeFromScopeLocked(tabId, "closed", true);
      }
      throw error;
    }
    await this.assertScoped(tabId);
    if (isTabRestricted(tab)) {
      await this.removeFromScopeLocked(tabId, "unshared", true);
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
    return this.runTabOperation(validTabId, async () => {
      const tab = await this.getTab(validTabId);
      if (isTabRestricted(tab)) {
        throw new ProtocolError(
          ERROR_CODES.RESTRICTED_URL,
          "Chrome does not allow sharing this URL.",
        );
      }
      if (this.scopedTabIds.has(validTabId)) {
        return {};
      }
      const scopedTab = await this.addToScopeLocked(tab);
      this.sendEvent(EVENTS.SCOPE_TAB_ADDED, this.publicTab(scopedTab, false));
      return {};
    });
  }

  async unshareTab(tabId) {
    await this.ready;
    const validTabId = requireTabId(tabId);
    return this.runTabOperation(validTabId, async () => {
      await this.assertScoped(validTabId);
      await this.removeFromScopeLocked(validTabId, "unshared", true);
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
    let tab;
    try {
      tab = await chrome.tabs.create({ url });
    } catch {
      throw new ProtocolError(
        ERROR_CODES.INTERNAL,
        "Chrome could not create the tab.",
      );
    }
    if (!Number.isInteger(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "Chrome did not return a tab identifier.",
      );
    }
    return this.runTabOperation(tab.id, async () => {
      const scopedTab = await this.addToScopeLocked(tab);
      this.sendEvent(EVENTS.SCOPE_TAB_ADDED, this.publicTab(scopedTab, false));
      return { tabId: tab.id };
    });
  }

  async remove(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.runTabOperation(tabId, async () => {
      await this.assertControllableLocked(tabId);
      try {
        await chrome.tabs.remove(tabId);
      } catch {
        if (!this.scopedTabIds.has(tabId)) {
          return {};
        }
        throw new ProtocolError(
          ERROR_CODES.TAB_NOT_FOUND,
          "The requested tab was not found.",
        );
      }
      if (this.scopedTabIds.has(tabId)) {
        await this.removeFromScopeLocked(tabId, "closed", false);
      }
      return {};
    });
  }

  async activate(args) {
    const values = requireArgs(args);
    const tabId = requireTabId(values.tabId);
    return this.runTabOperation(tabId, async () => {
      const tab = await this.assertControllableLocked(tabId);
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
      await this.assertControllableLocked(tabId);
      return {};
    });
  }

  async list() {
    await this.ready;
    const tabs = await this.collectScopedTabs(true);
    return { tabs };
  }

  async helloTabs() {
    await this.ready;
    return this.collectScopedTabs(false);
  }

  async handleDebuggerDetachLocked(tabId) {
    await this.ready;
    if (this.scopedTabIds.has(tabId)) {
      await this.removeFromScopeLocked(tabId, "detached", false);
    }
  }

  async handleTabCreated(tab) {
    await this.ready;
    if (!Number.isInteger(tab.id) || !Number.isInteger(tab.openerTabId)) {
      return;
    }
    await this.runTabOperation(tab.id, async () => {
      if (!this.scopedTabIds.has(tab.openerTabId) || isTabRestricted(tab)) {
        return;
      }
      const scopedTab = await this.addToScopeLocked(tab);
      this.sendEvent(EVENTS.TABS_CREATED, {
        tabId: scopedTab.id,
        openerTabId: tab.openerTabId,
        url: tabUrl(scopedTab),
      });
    });
  }

  async handleTabRemoved(tabId) {
    await this.ready;
    await this.runTabOperation(tabId, async () => {
      this.expectedGroupTransitions.delete(tabId);
      if (this.scopedTabIds.has(tabId)) {
        await this.removeFromScopeLocked(tabId, "closed", false);
      }
    });
  }

  async handleTabUpdated(tabId, changeInfo, expectedGroupTransition = null) {
    await this.ready;
    await this.runTabOperation(tabId, async () => {
      try {
        if (
          this.scopedTabIds.has(tabId) &&
          Object.hasOwn(changeInfo, "url") &&
          isRestrictedUrl(changeInfo.url)
        ) {
          await this.removeFromScopeLocked(tabId, "unshared", true);
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
        if (tab.groupId !== changeInfo.groupId) {
          return;
        }

        const controlledGroup = await this.getControlledGroup(tab.groupId);
        if (this.scopedTabIds.has(tabId)) {
          const expectedGroupId = this.scopedGroupIds.get(tabId);
          if (tab.groupId === expectedGroupId) {
            return;
          }
          if (controlledGroup !== null) {
            this.scopedGroupIds.set(tabId, tab.groupId);
            await this.persistScope();
            await this.updateControlledGroup(tab.groupId);
            return;
          }
          if (expectedGroupId !== undefined) {
            await this.removeFromScopeLocked(tabId, "unshared", true);
          }
          return;
        }

        if (controlledGroup === null) {
          return;
        }
        if (isTabRestricted(tab)) {
          await this.ungroupTabLocked(tabId, tab.groupId);
          return;
        }

        await this.updateControlledGroup(tab.groupId);
        const scopedTab = await this.addGroupedTabToScopeLocked(
          tab,
          tab.groupId,
        );
        this.sendEvent(
          EVENTS.SCOPE_TAB_ADDED,
          this.publicTab(scopedTab, false),
        );
      } finally {
        if (expectedGroupTransition !== null) {
          this.clearExpectedGroupTransition(tabId, expectedGroupTransition);
        }
      }
    });
  }

  async addToScopeLocked(tab) {
    if (!Number.isInteger(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "The requested tab was not found.",
      );
    }
    this.scopedTabIds.add(tab.id);
    await this.persistScope();
    await this.groupTabLocked(tab);
    return this.assertControllableLocked(tab.id);
  }

  async addGroupedTabToScopeLocked(tab, groupId) {
    if (!Number.isInteger(tab.id)) {
      throw new ProtocolError(
        ERROR_CODES.TAB_NOT_FOUND,
        "The requested tab was not found.",
      );
    }
    this.scopedTabIds.add(tab.id);
    this.scopedGroupIds.set(tab.id, groupId);
    await this.persistScope();
    return this.assertControllableLocked(tab.id);
  }

  async removeFromScopeLocked(tabId, reason, detach) {
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
    } finally {
      try {
        await this.persistScope();
      } finally {
        await this.ungroupTabLocked(tabId, scopedGroupId);
        this.sendEvent(EVENTS.SCOPE_TAB_REMOVED, { tabId, reason });
      }
    }
  }

  async groupTabLocked(tab) {
    const tabId = tab.id;
    if (!Number.isInteger(tabId) || !Number.isInteger(tab.windowId)) {
      return;
    }
    let groupId;
    try {
      const groups = await chrome.tabGroups.query({ windowId: tab.windowId });
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
      } finally {
        if (!grouped) {
          this.clearExpectedGroupTransition(tabId, transition);
        }
      }
    } catch {
      this.scopedGroupIds.delete(tabId);
      await this.persistScope();
      return;
    }
    this.scopedGroupIds.set(tabId, groupId);
    await this.persistScope();
    await this.updateControlledGroup(groupId);
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
    } catch {
      return;
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
    const tabs = [];
    for (const tabId of [...this.scopedTabIds]) {
      const tab = await this.runTabOperation(tabId, async () => {
        if (!this.scopedTabIds.has(tabId)) {
          return null;
        }
        let scopedTab;
        try {
          scopedTab = await chrome.tabs.get(tabId);
        } catch {
          await this.removeFromScopeLocked(tabId, "closed", false);
          return null;
        }
        if (isTabRestricted(scopedTab)) {
          await this.removeFromScopeLocked(tabId, "unshared", true);
          return null;
        }
        return this.publicTab(scopedTab, includeActive);
      });
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
    for (const tabId of [...this.scopedTabIds]) {
      await this.runTabOperation(tabId, async () => {
        let tab;
        try {
          tab = await chrome.tabs.get(tabId);
        } catch {
          await this.removeFromScopeLocked(tabId, "closed", false);
          return;
        }
        if (isTabRestricted(tab)) {
          await this.removeFromScopeLocked(tabId, "unshared", true);
          return;
        }
        const expectedGroupId = this.scopedGroupIds.get(tabId);
        if (expectedGroupId !== undefined && tab.groupId !== expectedGroupId) {
          const controlledGroup = await this.getControlledGroup(tab.groupId);
          if (controlledGroup === null) {
            await this.removeFromScopeLocked(tabId, "unshared", true);
          } else {
            this.scopedGroupIds.set(tabId, tab.groupId);
            await this.persistScope();
            await this.updateControlledGroup(tab.groupId);
          }
        } else if (expectedGroupId === undefined) {
          await this.groupTabLocked(tab);
        }
      });
    }
  }

  async runTabOperation(tabId, operation) {
    const previous = this.tabOperations.get(tabId) ?? Promise.resolve();
    const current = previous.catch(() => undefined).then(operation);
    this.tabOperations.set(tabId, current);
    try {
      return await current;
    } finally {
      if (this.tabOperations.get(tabId) === current) {
        this.tabOperations.delete(tabId);
      }
    }
  }

  async persistScope() {
    const groupIds = Object.fromEntries(this.scopedGroupIds);
    await chrome.storage.session.set({
      [SCOPED_TAB_IDS_KEY]: [...this.scopedTabIds],
      [SCOPED_GROUP_IDS_KEY]: groupIds,
    });
  }
}
