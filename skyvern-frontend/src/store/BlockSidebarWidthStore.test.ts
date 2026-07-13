// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import {
  useBlockSidebarWidthStore,
  BLOCK_SIDEBAR_WIDTH_MIN,
  BLOCK_SIDEBAR_WIDTH_MAX,
  BLOCK_SIDEBAR_WIDTH_DEFAULT,
  BLOCK_SIDEBAR_WIDTH_STORAGE_KEY,
} from "./BlockSidebarWidthStore";

beforeEach(() => {
  localStorage.clear();
  useBlockSidebarWidthStore.getState().reset();
});

describe("BlockSidebarWidthStore", () => {
  test("default width is BLOCK_SIDEBAR_WIDTH_DEFAULT", () => {
    expect(useBlockSidebarWidthStore.getState().width).toBe(
      BLOCK_SIDEBAR_WIDTH_DEFAULT,
    );
    expect(useBlockSidebarWidthStore.getState().renderedWidth).toBe(
      BLOCK_SIDEBAR_WIDTH_DEFAULT,
    );
  });

  test("setWidth clamps to [MIN, MAX]", () => {
    const store = useBlockSidebarWidthStore.getState();
    store.setWidth(50);
    expect(useBlockSidebarWidthStore.getState().width).toBe(
      BLOCK_SIDEBAR_WIDTH_MIN,
    );
    store.setWidth(99999);
    expect(useBlockSidebarWidthStore.getState().width).toBe(
      BLOCK_SIDEBAR_WIDTH_MAX,
    );
    store.setWidth(420);
    expect(useBlockSidebarWidthStore.getState().width).toBe(420);
  });

  test("setWidth persists to localStorage", () => {
    useBlockSidebarWidthStore.getState().setWidth(420);
    const raw = localStorage.getItem(BLOCK_SIDEBAR_WIDTH_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!)).toMatchObject({ state: { width: 420 } });
  });

  test("setRenderedWidth tracks the measured width without persisting it", () => {
    useBlockSidebarWidthStore.getState().setRenderedWidth(452);

    expect(useBlockSidebarWidthStore.getState().renderedWidth).toBe(452);
    const raw = localStorage.getItem(BLOCK_SIDEBAR_WIDTH_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!).state).not.toHaveProperty("renderedWidth");
  });

  test("reset returns width to default and clears persistence", () => {
    const store = useBlockSidebarWidthStore.getState();
    store.setWidth(420);
    store.reset();
    expect(useBlockSidebarWidthStore.getState().width).toBe(
      BLOCK_SIDEBAR_WIDTH_DEFAULT,
    );
    expect(useBlockSidebarWidthStore.getState().renderedWidth).toBe(
      BLOCK_SIDEBAR_WIDTH_DEFAULT,
    );
  });

  test("constants form a sane range", () => {
    expect(BLOCK_SIDEBAR_WIDTH_MIN).toBe(320);
    expect(BLOCK_SIDEBAR_WIDTH_MAX).toBe(640);
    // Matches the studio Copilot column width (STUDIO_COPILOT_WIDTH = 450).
    expect(BLOCK_SIDEBAR_WIDTH_DEFAULT).toBe(450);
    expect(BLOCK_SIDEBAR_WIDTH_DEFAULT).toBeGreaterThanOrEqual(
      BLOCK_SIDEBAR_WIDTH_MIN,
    );
    expect(BLOCK_SIDEBAR_WIDTH_DEFAULT).toBeLessThanOrEqual(
      BLOCK_SIDEBAR_WIDTH_MAX,
    );
  });

  describe("v0 -> v1 migration", () => {
    async function rehydrateFromV0(persistedWidth: number) {
      localStorage.setItem(
        BLOCK_SIDEBAR_WIDTH_STORAGE_KEY,
        JSON.stringify({ state: { width: persistedWidth }, version: 0 }),
      );
      await useBlockSidebarWidthStore.persist.rehydrate();
    }

    test("moves the old default (360) to the new default", async () => {
      await rehydrateFromV0(360);
      expect(useBlockSidebarWidthStore.getState().width).toBe(
        BLOCK_SIDEBAR_WIDTH_DEFAULT,
      );
    });

    test("keeps a deliberately-customized in-range width", async () => {
      await rehydrateFromV0(520);
      expect(useBlockSidebarWidthStore.getState().width).toBe(520);
    });

    test("resets an out-of-range width to the default", async () => {
      await rehydrateFromV0(9999);
      expect(useBlockSidebarWidthStore.getState().width).toBe(
        BLOCK_SIDEBAR_WIDTH_DEFAULT,
      );
    });
  });
});
