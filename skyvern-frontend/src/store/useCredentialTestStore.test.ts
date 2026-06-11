// @vitest-environment jsdom

import { afterEach, describe, expect, it } from "vitest";

import { useCredentialTestStore } from "./useCredentialTestStore";

const TEST = {
  credentialId: "cred_1",
  workflowRunId: "wr_1",
  url: "https://example.com/login",
  startTime: 1700000000000,
};

const NEWER_TEST = {
  credentialId: "cred_2",
  workflowRunId: "wr_2",
  url: "https://example.org/login",
  startTime: 1700000001000,
};

function persistedActiveTest() {
  const raw = localStorage.getItem("credential-test");
  return raw ? JSON.parse(raw).state.activeTest : undefined;
}

afterEach(() => {
  useCredentialTestStore.getState().clearActiveTest();
  localStorage.clear();
});

describe("useCredentialTestStore persistence (SKY-10855)", () => {
  it("persists the active test to localStorage so other tabs (noopener run tab) can rehydrate it", () => {
    useCredentialTestStore.getState().setActiveTest(TEST);

    expect(persistedActiveTest()).toEqual(TEST);
    expect(sessionStorage.getItem("credential-test")).toBeNull();
  });

  it("clears the persisted test when it reaches a terminal state", () => {
    useCredentialTestStore.getState().setActiveTest(TEST);
    useCredentialTestStore.getState().clearActiveTest();

    expect(persistedActiveTest()).toBeNull();
  });

  it("scoped clear ignores the slot when another run owns it", () => {
    useCredentialTestStore.getState().setActiveTest(NEWER_TEST);
    useCredentialTestStore.getState().clearActiveTest(TEST.workflowRunId);

    expect(useCredentialTestStore.getState().activeTest).toEqual(NEWER_TEST);
    expect(persistedActiveTest()).toEqual(NEWER_TEST);
  });

  it("scoped clear removes the slot when the run matches", () => {
    useCredentialTestStore.getState().setActiveTest(TEST);
    useCredentialTestStore.getState().clearActiveTest(TEST.workflowRunId);

    expect(useCredentialTestStore.getState().activeTest).toBeNull();
  });

  it("rehydrates from a storage event so tabs track each other's writes", () => {
    useCredentialTestStore.getState().setActiveTest(TEST);

    // Simulate another tab replacing the slot: write storage directly, then
    // dispatch the storage event (browsers only fire it in non-writer tabs).
    localStorage.setItem(
      "credential-test",
      JSON.stringify({ state: { activeTest: NEWER_TEST }, version: 0 }),
    );
    window.dispatchEvent(
      new StorageEvent("storage", { key: "credential-test" }),
    );

    expect(useCredentialTestStore.getState().activeTest).toEqual(NEWER_TEST);
  });
});
