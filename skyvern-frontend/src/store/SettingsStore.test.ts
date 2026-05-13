import { afterEach, describe, expect, it } from "vitest";

import { useSettingsStore } from "./SettingsStore";

const initialState = useSettingsStore.getState();

afterEach(() => {
  useSettingsStore.setState(initialState, true);
});

describe("SettingsStore.isLoadingABrowser", () => {
  it("defaults to false", () => {
    expect(useSettingsStore.getState().isLoadingABrowser).toBe(false);
  });

  it("flips to true when setIsLoadingABrowser(true) is called", () => {
    useSettingsStore.getState().setIsLoadingABrowser(true);
    expect(useSettingsStore.getState().isLoadingABrowser).toBe(true);
  });

  it("does not affect isUsingABrowser", () => {
    const before = useSettingsStore.getState().isUsingABrowser;
    useSettingsStore.getState().setIsLoadingABrowser(true);
    expect(useSettingsStore.getState().isUsingABrowser).toBe(before);
  });

  it("does not affect browserSessionId", () => {
    const before = useSettingsStore.getState().browserSessionId;
    useSettingsStore.getState().setIsLoadingABrowser(true);
    expect(useSettingsStore.getState().browserSessionId).toBe(before);
  });
});
