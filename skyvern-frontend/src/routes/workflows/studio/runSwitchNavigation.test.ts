import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  searchWithRunSwitched,
  useSwitchStudioRun,
} from "./runSwitchNavigation";

const { navigate } = vi.hoisted(() => ({ navigate: vi.fn() }));

vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigate };
});

function wrapperFor(entry: string) {
  return ({ children }: { children: ReactNode }) =>
    createElement(MemoryRouter, { initialEntries: [entry] }, children);
}

// The legacy run route carries the run in the path, so the search is empty —
// the case where reading the raw search would misjudge who owns the focus.
function pathRunWrapperFor(entry: string) {
  return ({ children }: { children: ReactNode }) =>
    createElement(
      MemoryRouter,
      { initialEntries: [entry] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: "/workflows/:workflowPermanentId/runs/:workflowRunId",
          element: children,
        }),
      ),
    );
}

beforeEach(() => {
  navigate.mockClear();
});

describe("searchWithRunSwitched system focus", () => {
  it("marks a system focus, and a user switch takes the marker back off", () => {
    const focused = searchWithRunSwitched("?panes=browser", "wr_1", {
      systemFocus: true,
    });
    expect(focused).toBe("?panes=browser&wr=wr_1&wrs=copilot");
    expect(searchWithRunSwitched(focused, "wr_2")).toBe("?wr=wr_2");
  });

  it("keeps the marker when the copilot hands off between its own runs", () => {
    expect(
      searchWithRunSwitched("?wr=wr_1&wrs=copilot", "wr_2", {
        systemFocus: true,
      }),
    ).toBe("?wr=wr_2&wrs=copilot");
  });

  it("leaves a run the user opened in the run class, unmarked", () => {
    // Marking here would flip the user out of the run layout they chose — the
    // remap the marker exists to prevent.
    expect(
      searchWithRunSwitched("?wr=wr_user", "wr_9", { systemFocus: true }),
    ).toBe("?wr=wr_9");
    expect(
      searchWithRunSwitched("?active=act_1", "wr_9", { systemFocus: true }),
    ).toBe("?wr=wr_9");
  });
});

describe("useSwitchStudioRun", () => {
  it("pushes a run the user picked, so Back returns to the previous one", () => {
    const { result } = renderHook(() => useSwitchStudioRun(), {
      wrapper: wrapperFor("/studio?panes=copilot,browser"),
    });
    result.current("wr_1");
    expect(navigate).toHaveBeenCalledWith(
      { search: "?wr=wr_1" },
      { replace: false },
    );
  });

  it("leaves a path-borne run of the user's in the run class, unmarked", () => {
    const { result } = renderHook(
      () => useSwitchStudioRun({ replace: true, systemFocus: true }),
      { wrapper: pathRunWrapperFor("/workflows/wpid_1/runs/wr_user") },
    );
    result.current("wr_test");
    expect(navigate).toHaveBeenCalledWith(
      { search: "?wr=wr_test" },
      { replace: true },
    );
  });

  it("replaces and marks a run focused on the user's behalf", () => {
    const { result } = renderHook(
      () => useSwitchStudioRun({ replace: true, systemFocus: true }),
      { wrapper: wrapperFor("/studio?panes=copilot,browser") },
    );
    result.current("wr_1");
    expect(navigate).toHaveBeenCalledWith(
      { search: "?panes=copilot,browser&wr=wr_1&wrs=copilot" },
      { replace: true },
    );
  });
});
