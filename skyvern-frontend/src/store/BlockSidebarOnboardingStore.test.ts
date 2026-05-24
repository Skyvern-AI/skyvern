// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import {
  useBlockSidebarOnboardingStore,
  BLOCK_SIDEBAR_ONBOARDING_STORAGE_KEY,
} from "./BlockSidebarOnboardingStore";

beforeEach(() => {
  localStorage.clear();
  useBlockSidebarOnboardingStore.getState().reset();
});

describe("BlockSidebarOnboardingStore", () => {
  test("hasSeenMigration starts false", () => {
    expect(useBlockSidebarOnboardingStore.getState().hasSeenMigration).toBe(
      false,
    );
  });

  test("markSeen sets the flag and persists", () => {
    useBlockSidebarOnboardingStore.getState().markSeen();
    expect(useBlockSidebarOnboardingStore.getState().hasSeenMigration).toBe(
      true,
    );
    const raw = localStorage.getItem(BLOCK_SIDEBAR_ONBOARDING_STORAGE_KEY);
    expect(raw).not.toBeNull();
    expect(JSON.parse(raw!)).toMatchObject({
      state: { hasSeenMigration: true },
    });
  });

  test("reset returns flag to false", () => {
    const store = useBlockSidebarOnboardingStore.getState();
    store.markSeen();
    store.reset();
    expect(useBlockSidebarOnboardingStore.getState().hasSeenMigration).toBe(
      false,
    );
  });
});
