// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { WorkflowScopeContext } from "./WorkflowScopeContext";
import { useUpdate } from "./useUpdate";

const updateNodeData = vi.fn();

vi.mock("@xyflow/react", () => ({
  useReactFlow: () => ({ updateNodeData }),
}));

afterEach(() => {
  vi.clearAllMocks();
});

function scopeWrapper(readOnly: boolean) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <WorkflowScopeContext.Provider value={{ workflowId: "w", readOnly }}>
        {children}
      </WorkflowScopeContext.Provider>
    );
  };
}

describe("useUpdate", () => {
  test("persists node data in the live editor scope", () => {
    const { result } = renderHook(
      () => useUpdate<{ x: string }>({ id: "n1", editable: true }),
      { wrapper: scopeWrapper(false) },
    );

    result.current({ x: "1" });

    expect(updateNodeData).toHaveBeenCalledWith("n1", { x: "1" });
  });

  // Reviewing a workflow version must never mutate the displayed snapshot.
  test("does not persist in a read-only comparison scope", () => {
    const { result } = renderHook(
      () => useUpdate<{ x: string }>({ id: "n1", editable: true }),
      { wrapper: scopeWrapper(true) },
    );

    result.current({ x: "1" });

    expect(updateNodeData).not.toHaveBeenCalled();
  });

  test("does not persist when the node is not editable", () => {
    const { result } = renderHook(
      () => useUpdate<{ x: string }>({ id: "n1", editable: false }),
      { wrapper: scopeWrapper(false) },
    );

    result.current({ x: "1" });

    expect(updateNodeData).not.toHaveBeenCalled();
  });
});
