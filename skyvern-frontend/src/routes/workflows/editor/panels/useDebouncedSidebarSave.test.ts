// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useSidebarSaveStateStore } from "@/store/SidebarSaveStateStore";

import { useDebouncedSidebarSave } from "./useDebouncedSidebarSave";

describe("useDebouncedSidebarSave (lightweight 'updated-at' tracker)", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useSidebarSaveStateStore.getState().reset();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  test("records lastUpdatedAt 300ms after the value changes", () => {
    const { rerender } = renderHook(
      ({ value }: { value: string }) =>
        useDebouncedSidebarSave({ blockId: "block-a", value }),
      { initialProps: { value: "v0" } },
    );

    rerender({ value: "v1" });
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-a"),
    ).toBeNull();

    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-a"),
    ).not.toBeNull();
  });

  test("does NOT mark updated when value reverts to the saved baseline", () => {
    const { rerender } = renderHook(
      ({ value }: { value: string }) =>
        useDebouncedSidebarSave({ blockId: "block-b", value }),
      { initialProps: { value: "v0" } },
    );

    rerender({ value: "v1" });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    const firstStamp = useSidebarSaveStateStore
      .getState()
      .getLastUpdatedAt("block-b");
    expect(firstStamp).not.toBeNull();

    rerender({ value: "v0" });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    const stampAfterRevert = useSidebarSaveStateStore
      .getState()
      .getLastUpdatedAt("block-b");
    expect(stampAfterRevert).toBe(firstStamp);
  });

  test("resets baseline when blockId changes (no false update on next mount)", () => {
    const { rerender } = renderHook(
      ({ blockId, value }: { blockId: string; value: string }) =>
        useDebouncedSidebarSave({ blockId, value }),
      { initialProps: { blockId: "block-c", value: "v-c" } },
    );

    rerender({ blockId: "block-d", value: "v-d" });
    act(() => {
      vi.advanceTimersByTime(300);
    });
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-d"),
    ).toBeNull();
  });

  test("commit() flushes the pending debounce and stamps lastUpdatedAt", () => {
    const { result, rerender } = renderHook(
      ({ value }: { value: string }) =>
        useDebouncedSidebarSave({ blockId: "block-e", value }),
      { initialProps: { value: "v0" } },
    );

    rerender({ value: "v1" });
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-e"),
    ).toBeNull();

    act(() => {
      result.current.commit();
    });
    expect(
      useSidebarSaveStateStore.getState().getLastUpdatedAt("block-e"),
    ).not.toBeNull();
  });
});
