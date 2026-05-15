import { beforeEach, describe, expect, it, vi } from "vitest";

const { storage } = vi.hoisted(() => {
  const storage = new Map<string, string>();
  const localStorageMock = {
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => {
      storage.set(key, value);
    },
    removeItem: (key: string) => {
      storage.delete(key);
    },
    clear: () => {
      storage.clear();
    },
    key: (index: number) => Array.from(storage.keys())[index] ?? null,
    get length() {
      return storage.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: localStorageMock,
    configurable: true,
    writable: true,
  });
  return { storage };
});

import {
  RUN_VIEWING_STORAGE_KEY,
  useRunViewingPreferenceStore,
} from "./RunViewingPreferenceStore";

describe("useRunViewingPreferenceStore", () => {
  beforeEach(() => {
    storage.clear();
    useRunViewingPreferenceStore.setState({ viewMode: "compact" }, false);
  });

  it("defaults viewMode to 'compact'", () => {
    expect(useRunViewingPreferenceStore.getState().viewMode).toBe("compact");
  });

  it("setViewMode persists to localStorage so the value survives a reload", () => {
    useRunViewingPreferenceStore.getState().setViewMode("detailed");

    expect(useRunViewingPreferenceStore.getState().viewMode).toBe("detailed");

    const raw = storage.get(RUN_VIEWING_STORAGE_KEY);
    expect(raw).toBeDefined();
    expect(JSON.parse(raw as string).state.viewMode).toBe("detailed");
  });

  it("reset clears the localStorage entry and reverts viewMode to 'compact'", () => {
    useRunViewingPreferenceStore.getState().setViewMode("detailed");
    expect(storage.has(RUN_VIEWING_STORAGE_KEY)).toBe(true);

    useRunViewingPreferenceStore.getState().reset();

    expect(useRunViewingPreferenceStore.getState().viewMode).toBe("compact");
    expect(storage.has(RUN_VIEWING_STORAGE_KEY)).toBe(false);
  });
});
