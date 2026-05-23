// @vitest-environment jsdom

import type { ReactNode } from "react";
import { beforeEach, describe, expect, test } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";

import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useCacheKeyValueUrlSync } from "./useCacheKeyValueUrlSync";

function makeWrapper(initialEntry: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter>
  );
}

function useTestHarness(ready: boolean = true) {
  useCacheKeyValueUrlSync(ready);
  return useLocation();
}

beforeEach(() => {
  useCacheKeyValueStore.getState().reset();
});

describe("useCacheKeyValueUrlSync", () => {
  test("preserves ?cache-key-value=foo when isExplicit=true", () => {
    useCacheKeyValueStore.getState().initialize("foo", true);
    const { result } = renderHook(useTestHarness, {
      wrapper: makeWrapper("/edit?cache-key-value=foo"),
    });
    expect(result.current.search).toBe("?cache-key-value=foo");
  });

  test("leaves URL empty when isExplicit=false and no param present", () => {
    const { result } = renderHook(useTestHarness, {
      wrapper: makeWrapper("/edit"),
    });
    expect(result.current.search).toBe("");
  });

  test("writes ?cache-key-value=bar after setExplicit(bar)", () => {
    const { result } = renderHook(useTestHarness, {
      wrapper: makeWrapper("/edit"),
    });
    act(() => {
      useCacheKeyValueStore.getState().setExplicit("bar");
    });
    expect(result.current.search).toBe("?cache-key-value=bar");
  });

  test("strips param when explicit value is set to empty string", () => {
    const { result } = renderHook(useTestHarness, {
      wrapper: makeWrapper("/edit"),
    });
    act(() => {
      useCacheKeyValueStore.getState().setExplicit("foo");
    });
    expect(result.current.search).toBe("?cache-key-value=foo");
    act(() => {
      useCacheKeyValueStore.getState().setExplicit("");
    });
    expect(result.current.search).toBe("");
  });

  test("strips ?cache-key-value=foo when isExplicit=false on mount", () => {
    const { result } = renderHook(useTestHarness, {
      wrapper: makeWrapper("/edit?cache-key-value=foo"),
    });
    expect(result.current.search).toBe("");
  });
});
