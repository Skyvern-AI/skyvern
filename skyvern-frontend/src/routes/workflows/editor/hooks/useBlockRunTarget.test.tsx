// @vitest-environment jsdom

import { renderHook } from "@testing-library/react";
import { type ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, test } from "vitest";

import { useBlockRunTarget } from "./useBlockRunTarget";

function wrapperAt(path: string, routePattern: string) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path={routePattern} element={<>{children}</>} />
        </Routes>
      </MemoryRouter>
    );
  };
}

describe("useBlockRunTarget", () => {
  test("resolves the legacy debugger's path params", () => {
    const { result } = renderHook(() => useBlockRunTarget(), {
      wrapper: wrapperAt(
        "/agents/wpid_1/wr_9/login-block/build",
        "/agents/:workflowPermanentId/:workflowRunId/:blockLabel/build",
      ),
    });
    expect(result.current).toEqual({
      workflowRunId: "wr_9",
      blockLabel: "login-block",
    });
  });

  test("resolves the studio's ?wr=/?bl= query params (running-chip + play-disable gate)", () => {
    // The parity bug: NodeHeader gated only on path params, so a studio block
    // run (?wr= active) left the block's running chip off and play enabled.
    const { result } = renderHook(() => useBlockRunTarget(), {
      wrapper: wrapperAt(
        "/agents/wpid_1/studio?wr=wr_9&bl=login-block&panes=editor,browser,overview",
        "/agents/:workflowPermanentId/studio",
      ),
    });
    expect(result.current).toEqual({
      workflowRunId: "wr_9",
      blockLabel: "login-block",
    });
  });

  test("a full studio run (?wr= without ?bl=) targets no block", () => {
    const { result } = renderHook(() => useBlockRunTarget(), {
      wrapper: wrapperAt(
        "/agents/wpid_1/studio?wr=wr_9",
        "/agents/:workflowPermanentId/studio",
      ),
    });
    expect(result.current).toEqual({
      workflowRunId: "wr_9",
      blockLabel: undefined,
    });
  });

  test("no run anywhere resolves to no target", () => {
    const { result } = renderHook(() => useBlockRunTarget(), {
      wrapper: wrapperAt(
        "/agents/wpid_1/edit",
        "/agents/:workflowPermanentId/edit",
      ),
    });
    expect(result.current).toEqual({
      workflowRunId: undefined,
      blockLabel: undefined,
    });
  });
});
