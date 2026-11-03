export class IndicatorState {
  constructor({ sendMessage, getCaptureTokens = () => [] }) {
    this.sendMessage = sendMessage;
    this.getCaptureTokens = getCaptureTokens;
    this.scopedTabs = new Set();
    this.connected = false;
    this.epoch = crypto.randomUUID();
    this.revision = 0;
  }

  isVisible(tabId) {
    return this.connected && this.scopedTabs.has(tabId);
  }

  state(tabId) {
    return {
      visible: this.isVisible(tabId),
      epoch: this.epoch,
      revision: this.revision,
    };
  }

  query(sender) {
    if (!Number.isInteger(sender.tab?.id) || sender.frameId !== 0) {
      return null;
    }
    return {
      ...this.state(sender.tab.id),
      captureTokens: this.getCaptureTokens(sender.tab.id),
    };
  }

  onScopeChange(tabId, scoped) {
    if (scoped) {
      this.scopedTabs.add(tabId);
    } else {
      this.scopedTabs.delete(tabId);
    }
    this.revision += 1;
    this.push(tabId);
  }

  setConnected(connected) {
    if (this.connected === connected) return;
    this.connected = connected;
    this.revision += 1;
    for (const tabId of this.scopedTabs) this.push(tabId);
  }

  push(tabId) {
    try {
      void Promise.resolve(
        this.sendMessage(tabId, {
          type: "skyvern.indicator.state",
          ...this.state(tabId),
        }),
      ).catch(() => undefined);
    } catch {}
  }
}
