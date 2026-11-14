import { act, renderHook } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { useTurnActivityChange } from "./useTurnActivityChange";

describe("useTurnActivityChange", () => {
  test("reports local turn start, terminal completion, and unmount", () => {
    const onTurnActivityChange = vi.fn();
    const { rerender, unmount } = renderHook(
      ({ active }) => useTurnActivityChange(active, onTurnActivityChange),
      { initialProps: { active: false } },
    );

    expect(onTurnActivityChange).toHaveBeenLastCalledWith(false);

    act(() => rerender({ active: true }));
    expect(onTurnActivityChange).toHaveBeenLastCalledWith(true);

    act(() => rerender({ active: false }));
    expect(onTurnActivityChange).toHaveBeenLastCalledWith(false);

    onTurnActivityChange.mockClear();
    unmount();
    expect(onTurnActivityChange).toHaveBeenCalledOnce();
    expect(onTurnActivityChange).toHaveBeenCalledWith(false);
  });
});
