import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useRowSelection } from "./useRowSelection";

type Row = { id: string };

const rows = (...ids: string[]): Row[] => ids.map((id) => ({ id }));

function setup(items: Row[], resetKey?: unknown, anchorResetKey?: unknown) {
  return renderHook(
    ({
      items,
      resetKey,
      anchorResetKey,
    }: {
      items: Row[];
      resetKey?: unknown;
      anchorResetKey?: unknown;
    }) =>
      useRowSelection({
        items,
        getId: (row: Row) => row.id,
        resetKey,
        anchorResetKey,
      }),
    { initialProps: { items, resetKey, anchorResetKey } },
  );
}

describe("useRowSelection", () => {
  it("toggles a single row and re-anchors", () => {
    const { result } = setup(rows("a", "b", "c"));
    act(() => result.current.handleSelect(1, false));
    expect(result.current.isSelected("b")).toBe(true);
    act(() => result.current.handleSelect(1, false));
    expect(result.current.isSelected("b")).toBe(false);
  });

  it("shift-click selects the range from the anchor in both directions", () => {
    const { result } = setup(rows("a", "b", "c", "d"));
    act(() => result.current.handleSelect(2, false));
    act(() => result.current.handleSelect(0, true));
    expect([...result.current.selected].sort()).toEqual(["a", "b", "c"]);
  });

  it("shift-click over a fully selected range deselects it", () => {
    const { result } = setup(rows("a", "b", "c"));
    act(() => result.current.handleSelect(0, false));
    act(() => result.current.handleSelect(2, true));
    act(() => result.current.handleSelect(2, true));
    expect(result.current.selected.size).toBe(0);
  });

  it("shift-click with no anchor falls back to a single toggle", () => {
    const { result } = setup(rows("a", "b", "c"));
    act(() => result.current.handleSelect(2, true));
    expect([...result.current.selected]).toEqual(["c"]);
  });

  it("toggleSelectAll selects everything, then clears; flags are correct midway", () => {
    const { result } = setup(rows("a", "b"));
    act(() => result.current.handleSelect(0, false));
    expect(result.current.someSelected).toBe(true);
    expect(result.current.allSelected).toBe(false);
    act(() => result.current.toggleSelectAll());
    expect(result.current.allSelected).toBe(true);
    expect(result.current.someSelected).toBe(false);
    act(() => result.current.toggleSelectAll());
    expect(result.current.selected.size).toBe(0);
  });

  it("resetKey change clears selection and anchor", () => {
    const { result, rerender } = setup(rows("a", "b", "c"), "k1");
    act(() => result.current.handleSelect(0, false));
    rerender({
      items: rows("a", "b", "c"),
      resetKey: "k2",
      anchorResetKey: undefined,
    });
    expect(result.current.selected.size).toBe(0);
    act(() => result.current.handleSelect(2, true));
    expect([...result.current.selected]).toEqual(["c"]);
  });

  it("anchorResetKey change clears only the anchor", () => {
    const { result, rerender } = setup(rows("a", "b", "c"), "k", "p1");
    act(() => result.current.handleSelect(0, false));
    rerender({
      items: rows("a", "b", "c"),
      resetKey: "k",
      anchorResetKey: "p2",
    });
    expect(result.current.isSelected("a")).toBe(true);
    act(() => result.current.handleSelect(2, true));
    expect([...result.current.selected].sort()).toEqual(["a", "c"]);
  });

  it("replaceSelection sets exactly the given ids and nulls the anchor", () => {
    const { result } = setup(rows("a", "b", "c"));
    act(() => result.current.handleSelect(0, false));
    act(() => result.current.replaceSelection(["b", "c"]));
    expect([...result.current.selected].sort()).toEqual(["b", "c"]);
    act(() => result.current.handleSelect(0, true));
    expect(result.current.isSelected("a")).toBe(true);
  });

  it("selectedItems keeps item order and drops stale ids", () => {
    const { result, rerender } = setup(rows("a", "b", "c"));
    act(() => result.current.toggleSelectAll());
    rerender({
      items: rows("c", "a"),
      resetKey: undefined,
      anchorResetKey: undefined,
    });
    expect(result.current.selectedItems.map((r) => r.id)).toEqual(["c", "a"]);
  });

  it("stale anchor beyond a shrunken list clamps instead of throwing", () => {
    const { result, rerender } = setup(rows("a", "b", "c", "d"));
    act(() => result.current.handleSelect(3, false));
    rerender({
      items: rows("a", "b"),
      resetKey: undefined,
      anchorResetKey: undefined,
    });
    act(() => result.current.handleSelect(0, true));
    expect(result.current.isSelected("a")).toBe(true);
  });
});
