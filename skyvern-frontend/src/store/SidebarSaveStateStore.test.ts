// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import { useSidebarSaveStateStore } from "./SidebarSaveStateStore";

beforeEach(() => {
  useSidebarSaveStateStore.getState().reset();
});

describe("SidebarSaveStateStore", () => {
  test("setLastUpdatedAt + getLastUpdatedAt round-trip per blockId", () => {
    const store = useSidebarSaveStateStore.getState();
    store.setLastUpdatedAt("block-a", 1000);
    store.setLastUpdatedAt("block-b", 2000);

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-a"),
    ).toBe(1000);
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-b"),
    ).toBe(2000);
  });

  test("getLastUpdatedAt returns null for unknown blockId", () => {
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("missing"),
    ).toBeNull();
  });

  test("getLastUpdatedAt returns null when blockId is null", () => {
    useSidebarSaveStateStore.getState().setLastUpdatedAt("block-a", 1000);
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt(null),
    ).toBeNull();
  });

  test("setLastUpdatedAt overwrites the previous timestamp for the same blockId", () => {
    const store = useSidebarSaveStateStore.getState();
    store.setLastUpdatedAt("block-a", 1000);
    store.setLastUpdatedAt("block-a", 5000);

    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-a"),
    ).toBe(5000);
  });

  test("reset clears all entries", () => {
    const store = useSidebarSaveStateStore.getState();
    store.setLastUpdatedAt("block-a", 1000);
    store.setLastUpdatedAt("block-b", 2000);

    store.reset();

    expect(useSidebarSaveStateStore.getState().lastUpdatedAt).toEqual({});
  });
});
