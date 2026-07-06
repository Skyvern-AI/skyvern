import { afterEach, describe, expect, it, vi } from "vitest";

import { useStudioBrowserStore } from "./useStudioBrowserStore";

const initialState = useStudioBrowserStore.getState();

afterEach(() => {
  useStudioBrowserStore.setState(initialState, true);
});

describe("useStudioBrowserStore activity indicator state", () => {
  it("does not show unseen activity by default", () => {
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("marks and clears unseen browser activity", () => {
    useStudioBrowserStore.getState().markActivity();
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(true);

    useStudioBrowserStore.getState().clearActivity();
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });

  it("does not notify subscribers when activity is already in the target state", () => {
    const listener = vi.fn();
    const unsubscribe = useStudioBrowserStore.subscribe(listener);

    useStudioBrowserStore.getState().clearActivity();
    expect(listener).not.toHaveBeenCalled();

    useStudioBrowserStore.getState().markActivity();
    expect(listener).toHaveBeenCalledTimes(1);

    useStudioBrowserStore.getState().markActivity();
    expect(listener).toHaveBeenCalledTimes(1);

    useStudioBrowserStore.getState().clearActivity();
    expect(listener).toHaveBeenCalledTimes(2);

    useStudioBrowserStore.getState().clearActivity();
    expect(listener).toHaveBeenCalledTimes(2);

    unsubscribe();
  });

  it("clears unseen activity when the browser state resets", () => {
    useStudioBrowserStore.getState().setStreamUrl("https://example.test");
    useStudioBrowserStore.getState().markActivity();

    useStudioBrowserStore.getState().reset();

    expect(useStudioBrowserStore.getState().streamUrl).toBeNull();
    expect(useStudioBrowserStore.getState().hasUnseenActivity).toBe(false);
  });
});
