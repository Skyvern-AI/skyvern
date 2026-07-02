// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { WorkflowScopeContext } from "../WorkflowScopeContext";
import { useWorkflowEditorMode } from "./useWorkflowEditorMode";

function wrapper(initial: string, readOnly = false) {
  return ({ children }: { children: React.ReactNode }) => (
    <MemoryRouter initialEntries={[initial]}>
      <WorkflowScopeContext.Provider
        value={{ workflowId: "wpid_abc", readOnly }}
      >
        {children}
      </WorkflowScopeContext.Provider>
    </MemoryRouter>
  );
}

describe("useWorkflowEditorMode", () => {
  test("returns 'edit' for /workflows/wpid/edit", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/edit"),
    });
    expect(result.current).toBe("edit");
  });

  test("returns 'build' for /workflows/wpid/build", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/build"),
    });
    expect(result.current).toBe("build");
  });

  test("returns 'build' for the studio editor at /workflows/wpid/studio", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/studio"),
    });
    expect(result.current).toBe("build");
  });

  test("returns 'edit' for a read-only comparison canvas at /studio", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/studio", true),
    });
    expect(result.current).toBe("edit");
  });

  test("returns 'build' for /workflows/wpid/runId/blockLabel/build", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/wfr_xyz/block_1/build"),
    });
    expect(result.current).toBe("build");
  });

  test("recognizes /edit even with a trailing slash or subpath", () => {
    const { result: trailing } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/edit/"),
    });
    expect(trailing.current).toBe("edit");

    const { result: subpath } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/edit/something"),
    });
    expect(subpath.current).toBe("edit");
  });

  test("recognizes /studio even with a trailing slash or subpath", () => {
    const { result: trailing } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/studio/"),
    });
    expect(trailing.current).toBe("build");

    const { result: subpath } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/studio/something"),
    });
    expect(subpath.current).toBe("build");
  });

  test("defaults to 'build' for paths that match neither", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_abc/runs"),
    });
    expect(result.current).toBe("build");
  });

  test("does not match unrelated paths that happen to contain 'edit'", () => {
    const { result } = renderHook(() => useWorkflowEditorMode(), {
      wrapper: wrapper("/workflows/wpid_credit_card/build"),
    });
    expect(result.current).toBe("build");
  });
});
