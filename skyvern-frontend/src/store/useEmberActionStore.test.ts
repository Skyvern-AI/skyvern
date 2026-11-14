import { beforeEach, describe, expect, test } from "vitest";

import { useEmberActionStore } from "./useEmberActionStore";

beforeEach(() => {
  useEmberActionStore.setState({
    pendingBuild: null,
    generatingBlockLabel: null,
    cancelNonce: 0,
  });
});

describe("useEmberActionStore", () => {
  test("requestBuild arms a pending build and marks the block generating", () => {
    useEmberActionStore
      .getState()
      .requestBuild({ blockLabel: "open_page", prompt: "open the page" });

    const state = useEmberActionStore.getState();
    expect(state.pendingBuild).toEqual({
      blockLabel: "open_page",
      prompt: "open the page",
    });
    expect(state.generatingBlockLabel).toBe("open_page");
  });

  test("requestCancel clears the pending build so a queued build cannot still arm", () => {
    const store = useEmberActionStore.getState();
    store.requestBuild({ blockLabel: "open_page", prompt: "open the page" });

    store.requestCancel();

    const state = useEmberActionStore.getState();
    expect(state.pendingBuild).toBeNull();
    expect(state.generatingBlockLabel).toBeNull();
    expect(state.cancelNonce).toBe(1);
  });
});
