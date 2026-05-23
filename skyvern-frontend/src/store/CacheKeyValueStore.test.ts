// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import { useCacheKeyValueStore } from "./CacheKeyValueStore";

beforeEach(() => {
  useCacheKeyValueStore.getState().reset();
});

describe("CacheKeyValueStore", () => {
  test("initial state is empty value, null filter, not explicit", () => {
    const state = useCacheKeyValueStore.getState();
    expect(state.cacheKeyValue).toBe("");
    expect(state.filter).toBeNull();
    expect(state.isExplicit).toBe(false);
  });

  test("initialize seeds value and explicit flag", () => {
    useCacheKeyValueStore.getState().initialize("seed-value", true);
    const state = useCacheKeyValueStore.getState();
    expect(state.cacheKeyValue).toBe("seed-value");
    expect(state.isExplicit).toBe(true);
    expect(state.filter).toBeNull();
  });

  test("initialize with isExplicit=false leaves the flag false", () => {
    useCacheKeyValueStore.getState().initialize("auto-computed", false);
    expect(useCacheKeyValueStore.getState().isExplicit).toBe(false);
  });

  test("setExplicit sets value AND raises the explicit flag", () => {
    useCacheKeyValueStore.getState().setExplicit("user-typed");
    const state = useCacheKeyValueStore.getState();
    expect(state.cacheKeyValue).toBe("user-typed");
    expect(state.isExplicit).toBe(true);
  });

  test("setFilter updates filter only", () => {
    useCacheKeyValueStore.getState().initialize("seed", true);
    useCacheKeyValueStore.getState().setFilter("part");
    const state = useCacheKeyValueStore.getState();
    expect(state.filter).toBe("part");
    expect(state.cacheKeyValue).toBe("seed");
    expect(state.isExplicit).toBe(true);
  });

  test("setFilter accepts null to clear", () => {
    useCacheKeyValueStore.getState().setFilter("part");
    useCacheKeyValueStore.getState().setFilter(null);
    expect(useCacheKeyValueStore.getState().filter).toBeNull();
  });

  test("reset returns to defaults", () => {
    useCacheKeyValueStore.getState().setExplicit("x");
    useCacheKeyValueStore.getState().setFilter("y");
    useCacheKeyValueStore.getState().reset();
    const state = useCacheKeyValueStore.getState();
    expect(state.cacheKeyValue).toBe("");
    expect(state.filter).toBeNull();
    expect(state.isExplicit).toBe(false);
  });
});
