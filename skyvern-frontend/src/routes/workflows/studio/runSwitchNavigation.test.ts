import { renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  searchWithEmberBrowserFocus,
  searchWithEmberBrowserReleased,
  searchWithoutRun,
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

describe("searchWithoutRun", () => {
  it("drops the inspected run and its selection but keeps the pane layout", () => {
    expect(
      searchWithoutRun("?panes=browser&wr=wr_1&wrs=ember&active=b1&bl=x"),
    ).toBe("?panes=browser");
  });
});

describe("searchWithRunSwitched system focus", () => {
  it("marks a system focus, and a user switch takes the marker back off", () => {
    const focused = searchWithRunSwitched("?panes=browser", "wr_1", {
      systemFocus: true,
    });
    expect(focused).toBe("?panes=browser&wr=wr_1&wrs=ember");
    expect(searchWithRunSwitched(focused, "wr_2")).toBe("?wr=wr_2");
  });

  it("keeps the marker when the Ember hands off between its own runs", () => {
    expect(
      searchWithRunSwitched("?wr=wr_1&wrs=ember", "wr_2", {
        systemFocus: true,
      }),
    ).toBe("?wr=wr_2&wrs=ember");
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

describe("searchWithEmberBrowserFocus", () => {
  it("pins an Ember-focused run's pane to the browser the call names", () => {
    expect(searchWithEmberBrowserFocus("?wr=wr_1&wrs=ember", "pbs_debug")).toBe(
      "?wr=wr_1&wrs=pbs_debug",
    );
  });

  it("follows the latest call when it names a different browser", () => {
    const onDebug = searchWithEmberBrowserFocus(
      "?wr=wr_1&wrs=ember",
      "pbs_debug",
    );
    const onRun = searchWithEmberBrowserFocus(onDebug, "pbs_run");
    expect(onRun).toBe("?wr=wr_1&wrs=pbs_run");
    expect(searchWithEmberBrowserFocus(onRun, "pbs_debug")).toBe(
      "?wr=wr_1&wrs=pbs_debug",
    );
  });

  it("does not move on a call that carries no browser session, whatever its text", () => {
    for (const callSession of [null, undefined, ""]) {
      expect(
        searchWithEmberBrowserFocus("?wr=wr_1&wrs=pbs_debug", callSession),
      ).toBe("?wr=wr_1&wrs=pbs_debug");
    }
  });

  it("never moves a run the user opened", () => {
    expect(searchWithEmberBrowserFocus("?wr=wr_user", "pbs_debug")).toBe(
      "?wr=wr_user",
    );
  });

  it("hands the pane back to the next run Ember starts", () => {
    const inspecting = searchWithEmberBrowserFocus(
      "?wr=wr_1&wrs=ember",
      "pbs_debug",
    );
    expect(
      searchWithRunSwitched(inspecting, "wr_2", { systemFocus: true }),
    ).toBe("?wr=wr_2&wrs=ember");
  });
});

describe("searchWithEmberBrowserReleased", () => {
  it("returns a pinned pane to the Ember run focus", () => {
    expect(searchWithEmberBrowserReleased("?wr=wr_1&wrs=pbs_debug")).toBe(
      "?wr=wr_1&wrs=ember",
    );
  });

  it("leaves an unpinned or user-opened run alone", () => {
    for (const search of ["?wr=wr_1&wrs=ember", "?wr=wr_user", ""]) {
      expect(searchWithEmberBrowserReleased(search)).toBe(search);
    }
  });
});

describe("useSwitchStudioRun", () => {
  it("pushes a run the user picked, so Back returns to the previous one", () => {
    const { result } = renderHook(() => useSwitchStudioRun(), {
      wrapper: wrapperFor("/studio?panes=ember,browser"),
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
      { wrapper: wrapperFor("/studio?panes=ember,browser") },
    );
    result.current("wr_1");
    expect(navigate).toHaveBeenCalledWith(
      { search: "?panes=ember,browser&wr=wr_1&wrs=ember" },
      { replace: true },
    );
  });
});
