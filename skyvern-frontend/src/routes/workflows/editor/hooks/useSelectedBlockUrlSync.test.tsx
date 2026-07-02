// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { beforeEach, describe, expect, test } from "vitest";

import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";

import type { AppNode } from "../nodes";
import {
  getInitialSelectedBlockId,
  SELECTED_BLOCK_SEARCH_PARAM,
  useSelectedBlockUrlSync,
} from "./useSelectedBlockUrlSync";

const startNode = {
  id: "start-node",
  type: "start",
  data: { label: "__start_block__" },
} as AppNode;

const loginNode = {
  id: "login-node",
  type: "task",
  data: { label: "Login" },
} as AppNode;

const checkoutNode = {
  id: "checkout-node",
  type: "codeBlock",
  data: { label: "Checkout step" },
} as AppNode;

const nodes = [startNode, loginNode, checkoutNode];

function makeWrapper(initialEntry: string) {
  return ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter>
  );
}

function useTestHarness(enabled = true) {
  useSelectedBlockUrlSync({ enabled, nodes });
  return useLocation();
}

function useTestHarnessWithNavigate(enabled = true) {
  useSelectedBlockUrlSync({ enabled, nodes });
  return { location: useLocation(), navigate: useNavigate() };
}

beforeEach(() => {
  useWorkflowPanelStore.getState().setSelectedBlockId(null);
});

describe("getInitialSelectedBlockId", () => {
  test("restores the matching workflow block in studio", () => {
    const searchParams = new URLSearchParams();
    searchParams.set(SELECTED_BLOCK_SEARCH_PARAM, "Login");

    expect(
      getInitialSelectedBlockId({ enabled: true, nodes, searchParams }),
    ).toBe("login-node");
  });

  test("falls back to the start node when the URL block is gone", () => {
    const searchParams = new URLSearchParams();
    searchParams.set(SELECTED_BLOCK_SEARCH_PARAM, "Removed block");

    expect(
      getInitialSelectedBlockId({ enabled: true, nodes, searchParams }),
    ).toBe("start-node");
  });

  test("keeps legacy editor selection empty", () => {
    const searchParams = new URLSearchParams();
    searchParams.set(SELECTED_BLOCK_SEARCH_PARAM, "Login");

    expect(
      getInitialSelectedBlockId({ enabled: false, nodes, searchParams }),
    ).toBeNull();
  });
});

describe("useSelectedBlockUrlSync", () => {
  test("restores selectedBlockId from the selected-block search param", async () => {
    useWorkflowPanelStore.getState().setSelectedBlockId("start-node");

    const { result } = renderHook(() => useTestHarness(), {
      wrapper: makeWrapper("/workflows/wpid_abc/studio?selected-block=Login"),
    });

    await waitFor(() => {
      expect(useWorkflowPanelStore.getState().selectedBlockId).toBe(
        "login-node",
      );
    });
    expect(result.current.search).toBe("?selected-block=Login");
  });

  test("mirrors block selection to the URL without dropping existing params", async () => {
    const { result } = renderHook(() => useTestHarness(), {
      wrapper: makeWrapper("/workflows/wpid_abc/studio?wr=run_1"),
    });

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("checkout-node");
    });

    await waitFor(() => {
      expect(result.current.search).toBe(
        "?wr=run_1&selected-block=Checkout+step",
      );
    });
  });

  test("removes the selected-block param when the selected block is missing", async () => {
    const { result } = renderHook(() => useTestHarness(), {
      wrapper: makeWrapper(
        "/workflows/wpid_abc/studio?selected-block=Removed+block",
      ),
    });

    await waitFor(() => {
      expect(result.current.search).toBe("");
    });
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBe("start-node");
  });

  test("clears a stale selected-block param instead of re-writing the previous selection", async () => {
    const { result } = renderHook(() => useTestHarnessWithNavigate(), {
      wrapper: makeWrapper("/workflows/wpid_abc/studio?selected-block=Login"),
    });

    await waitFor(() => {
      expect(useWorkflowPanelStore.getState().selectedBlockId).toBe(
        "login-node",
      );
    });

    act(() => {
      result.current.navigate(
        "/workflows/wpid_abc/studio?selected-block=Removed+block",
      );
    });

    await waitFor(() => {
      expect(result.current.location.search).toBe("");
    });
    // The param must stay cleared: selectedBlockId has to actually move off
    // "login-node", otherwise the mirror effect writes "Login" right back.
    expect(useWorkflowPanelStore.getState().selectedBlockId).toBe("start-node");
    expect(result.current.location.search).toBe("");
  });

  test("merges mirror writes against the live URL so a concurrent ?panes= write survives", async () => {
    // Simulate a pane toggle whose navigate() already hit the real URL while
    // this render's closure params still predate it (the stale-prev race).
    window.history.replaceState(
      null,
      "",
      "/workflows/wpid_abc/studio?wr=run_1&panes=copilot,editor",
    );
    try {
      const { result } = renderHook(() => useTestHarness(), {
        wrapper: makeWrapper("/workflows/wpid_abc/studio?wr=run_1"),
      });

      act(() => {
        useWorkflowPanelStore.getState().setSelectedBlockId("checkout-node");
      });

      await waitFor(() => {
        expect(result.current.search).toContain("selected-block=Checkout+step");
      });
      const params = new URLSearchParams(result.current.search);
      expect(params.get("panes")).toBe("copilot,editor");
      expect(params.get("wr")).toBe("run_1");
    } finally {
      window.history.replaceState(null, "", "/");
    }
  });

  test("does not touch the URL outside the embedded studio", async () => {
    const { result } = renderHook(() => useTestHarness(false), {
      wrapper: makeWrapper("/workflows/wpid_abc/edit"),
    });

    act(() => {
      useWorkflowPanelStore.getState().setSelectedBlockId("login-node");
    });

    await waitFor(() => {
      expect(result.current.search).toBe("");
    });
  });
});
