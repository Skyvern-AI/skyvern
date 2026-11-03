import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { useHistoryLoad } from "./useHistoryLoad";

describe("useHistoryLoad", () => {
  it("keeps the flag raised when a superseded load finishes while a newer load is in flight", async () => {
    const { result } = renderHook(() => useHistoryLoad());
    const load = async (fetched: Promise<void>) => {
      const seq = result.current.beginHistoryLoad();
      try {
        await fetched;
      } finally {
        result.current.endHistoryLoad(seq);
      }
    };
    let finishFirst!: () => void;
    let finishSecond!: () => void;
    let first!: Promise<void>;
    let second!: Promise<void>;

    act(() => {
      first = load(new Promise((resolve) => (finishFirst = resolve)));
    });
    act(() => {
      second = load(new Promise((resolve) => (finishSecond = resolve)));
    });
    expect(result.current.isLoadingHistory).toBe(true);

    await act(async () => {
      finishFirst();
      await first;
    });
    expect(result.current.isLoadingHistory).toBe(true);

    await act(async () => {
      finishSecond();
      await second;
    });
    expect(result.current.isLoadingHistory).toBe(false);
  });
});
