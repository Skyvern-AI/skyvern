import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  SESSION_INTERACTED_KEY,
  useHasInteractedThisSession,
} from "./useHasInteractedThisSession";

const SCOPE_SELECTOR = '[data-testid="block-config-sidebar"]';

beforeEach(() => {
  sessionStorage.clear();
  document.body.innerHTML = `<div data-testid="block-config-sidebar"><input id="inside" /></div><input id="outside" />`;
});

afterEach(() => {
  document.body.innerHTML = "";
});

describe("useHasInteractedThisSession", () => {
  it("returns false on first mount when sessionStorage is empty", () => {
    const { result } = renderHook(() => useHasInteractedThisSession());
    expect(result.current).toBe(false);
  });

  it("returns true if sessionStorage already records prior interaction", () => {
    sessionStorage.setItem(SESSION_INTERACTED_KEY, "true");
    const { result } = renderHook(() => useHasInteractedThisSession());
    expect(result.current).toBe(true);
  });

  it("flips to true on input event inside the sidebar scope and persists to sessionStorage", () => {
    const { result } = renderHook(() => useHasInteractedThisSession());
    expect(result.current).toBe(false);
    const inside = document.querySelector(`${SCOPE_SELECTOR} #inside`);
    expect(inside).not.toBeNull();
    act(() => {
      inside!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(result.current).toBe(true);
    expect(sessionStorage.getItem(SESSION_INTERACTED_KEY)).toBe("true");
  });

  it("ignores input events outside the sidebar scope", () => {
    const { result } = renderHook(() => useHasInteractedThisSession());
    const outside = document.querySelector("#outside");
    expect(outside).not.toBeNull();
    act(() => {
      outside!.dispatchEvent(new Event("input", { bubbles: true }));
    });
    expect(result.current).toBe(false);
    expect(sessionStorage.getItem(SESSION_INTERACTED_KEY)).toBe(null);
  });
});
