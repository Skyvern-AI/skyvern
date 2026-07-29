// @vitest-environment jsdom

import { act, renderHook } from "@testing-library/react";
import type { SetURLSearchParams } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { useRunTagFilterParam } from "./useRunTagFilterParam";

describe("useRunTagFilterParam", () => {
  it("parses repeated params and writes or clears one canonical filter", () => {
    const searchParams = new URLSearchParams(
      "tags=env%3Aprod&tags=standalone%2Cinvalid%3A&page=4&status=completed",
    );
    const setSearchParams: SetURLSearchParams = vi.fn();
    const { result } = renderHook(() =>
      useRunTagFilterParam(searchParams, setSearchParams),
    );

    expect(result.current.tagTerms).toEqual([
      { key: "env", value: "prod" },
      { key: null, value: "standalone" },
    ]);
    expect(result.current.tagsParam).toBe("standalone,env:prod");

    act(() => {
      result.current.writeTagsParam([
        { key: "team", value: "qa" },
        { key: null, value: "urgent" },
      ]);
    });

    const firstCall = vi.mocked(setSearchParams).mock.calls[0];
    expect(firstCall).toBeDefined();
    const writtenParams = firstCall?.[0];
    const options = firstCall?.[1];
    expect(writtenParams).toBeInstanceOf(URLSearchParams);
    expect((writtenParams as URLSearchParams).getAll("tags")).toEqual([
      "urgent,team:qa",
    ]);
    expect((writtenParams as URLSearchParams).get("page")).toBe("1");
    expect((writtenParams as URLSearchParams).get("status")).toBe("completed");
    expect(options).toEqual({ replace: true });

    act(() => {
      result.current.writeTagsParam([]);
    });

    const clearedParams = vi.mocked(setSearchParams).mock.calls[1]?.[0];
    expect((clearedParams as URLSearchParams).has("tags")).toBe(false);
    expect((clearedParams as URLSearchParams).get("page")).toBe("1");
    expect((clearedParams as URLSearchParams).get("status")).toBe("completed");
  });
});
