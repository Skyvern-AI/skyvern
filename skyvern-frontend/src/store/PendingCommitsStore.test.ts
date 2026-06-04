// @vitest-environment jsdom

import { beforeEach, describe, expect, test, vi } from "vitest";

import { usePendingCommitsStore } from "./PendingCommitsStore";

beforeEach(() => {
  usePendingCommitsStore.setState({ commits: {} });
});

describe("PendingCommitsStore (SKY-9362)", () => {
  test("register stores the commit fn under the blockId", () => {
    const fn = vi.fn(() => true);
    usePendingCommitsStore.getState().register("block-a", fn);

    expect(usePendingCommitsStore.getState().commits["block-a"]).toBe(fn);
  });

  test("flush invokes the registered commit and returns its result", () => {
    const fn = vi.fn(() => true);
    usePendingCommitsStore.getState().register("block-a", fn);

    const result = usePendingCommitsStore.getState().flush("block-a");

    expect(fn).toHaveBeenCalledTimes(1);
    expect(result).toBe(true);
  });

  test("flush forwards a false return when commit reports invalid", () => {
    usePendingCommitsStore.getState().register("block-a", () => false);

    expect(usePendingCommitsStore.getState().flush("block-a")).toBe(false);
  });

  test("flush returns true (no-op) for unregistered blockId", () => {
    expect(usePendingCommitsStore.getState().flush("missing")).toBe(true);
  });

  test("flush returns true (no-op) when blockId is null", () => {
    expect(usePendingCommitsStore.getState().flush(null)).toBe(true);
  });

  test("unregister removes the commit entry for that blockId only", () => {
    const a = vi.fn(() => true);
    const b = vi.fn(() => true);
    usePendingCommitsStore.getState().register("block-a", a);
    usePendingCommitsStore.getState().register("block-b", b);

    usePendingCommitsStore.getState().unregister("block-a");

    expect(
      usePendingCommitsStore.getState().commits["block-a"],
    ).toBeUndefined();
    expect(usePendingCommitsStore.getState().commits["block-b"]).toBe(b);
  });

  test("register replaces a prior commit fn for the same blockId", () => {
    const first = vi.fn(() => true);
    const second = vi.fn(() => true);
    usePendingCommitsStore.getState().register("block-a", first);
    usePendingCommitsStore.getState().register("block-a", second);

    usePendingCommitsStore.getState().flush("block-a");

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });
});
