// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { useWorkflowEditorMode } from "./useWorkflowEditorMode";

function wrapper(initial: string) {
  return ({ children }: { children: React.ReactNode }) => (
    <MemoryRouter initialEntries={[initial]}>{children}</MemoryRouter>
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
