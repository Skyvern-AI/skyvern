// @vitest-environment jsdom

import { beforeEach, describe, expect, test } from "vitest";

import { useShowAllCodeStore } from "./ShowAllCodeStore";

beforeEach(() => {
  useShowAllCodeStore.getState().reset();
});

describe("ShowAllCodeStore", () => {
  test("initial showAllCode is false", () => {
    expect(useShowAllCodeStore.getState().showAllCode).toBe(false);
  });

  test("setShowAllCode updates the flag", () => {
    useShowAllCodeStore.getState().setShowAllCode(true);
    expect(useShowAllCodeStore.getState().showAllCode).toBe(true);
  });

  test("toggleShowAllCode flips the flag", () => {
    useShowAllCodeStore.getState().toggleShowAllCode();
    expect(useShowAllCodeStore.getState().showAllCode).toBe(true);
    useShowAllCodeStore.getState().toggleShowAllCode();
    expect(useShowAllCodeStore.getState().showAllCode).toBe(false);
  });

  test("reset returns to false", () => {
    useShowAllCodeStore.getState().setShowAllCode(true);
    useShowAllCodeStore.getState().reset();
    expect(useShowAllCodeStore.getState().showAllCode).toBe(false);
  });
});
