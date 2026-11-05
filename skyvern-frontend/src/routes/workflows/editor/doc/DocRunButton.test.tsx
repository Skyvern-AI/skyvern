// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";

const { toastSpy, docRunState } = vi.hoisted(() => ({
  toastSpy: vi.fn(),
  docRunState: { current: {} as Record<string, unknown> },
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: toastSpy }));
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));
vi.mock("./useDocRun", () => ({ useDocRun: () => docRunState.current }));

import { DocRunButton } from "./DocRunButton";

const block = {
  label: "open_site",
  status: Status.Completed,
} as WorkflowRunBlock;

function setRun(status: Status, extra: Record<string, unknown> = {}) {
  const live = status === Status.Running;
  docRunState.current = {
    runId: "wr_1",
    run: { workflow_run_id: "wr_1", status, ...extra },
    blocksByLabel: live ? new Map() : new Map([["open_site", block]]),
    loopsByLabel: new Map(),
    live,
    owned: true,
    blockRun: false,
  };
}

function renderAt(path: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const tree = () => (
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path="/workflows/:workflowPermanentId/studio"
              element={<DocRunButton />}
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>
  );
  const view = render(tree());
  return () => view.rerender(tree());
}

// Each case uses its own run id: the shared completion-notice claim is module state.
let seq = 0;
function endRun(path: string, extra: Record<string, unknown> = {}) {
  seq += 1;
  const id = `wr_${seq}`;
  setRun(Status.Running, extra);
  docRunState.current.runId = id;
  const rerender = renderAt(path.replace("wr_x", id));
  setRun(Status.Completed, extra);
  docRunState.current.runId = id;
  act(() => rerender());
}

describe("DocRunButton end-of-run toast", () => {
  beforeEach(() => toastSpy.mockClear());
  afterEach(cleanup);

  it("announces the Doc's own run once, and leaves URL and Copilot runs to their own notices", () => {
    endRun("/workflows/wpid_1/studio");
    expect(toastSpy).toHaveBeenCalledTimes(1);
    expect(toastSpy.mock.calls[0]![0]).toMatchObject({ title: "Run finished" });
    cleanup();

    toastSpy.mockClear();
    // A run named in ?wr= gets the studio's shared end-of-run toast.
    endRun("/workflows/wpid_1/studio?wr=wr_x");
    expect(toastSpy).not.toHaveBeenCalled();
    cleanup();

    // Copilot reports its own test runs in the chat card.
    endRun("/workflows/wpid_1/studio", { copilot_session_id: "cs_1" });
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it("offers no Stop and no end toast for a run from elsewhere that is still going", () => {
    setRun(Status.Running);
    docRunState.current.owned = false;
    const rerender = renderAt("/workflows/wpid_1/studio");
    expect(screen.queryByRole("button", { name: /stop/i })).toBeNull();
    setRun(Status.Completed);
    docRunState.current.owned = false;
    act(() => rerender());
    expect(toastSpy).not.toHaveBeenCalled();
  });
});
