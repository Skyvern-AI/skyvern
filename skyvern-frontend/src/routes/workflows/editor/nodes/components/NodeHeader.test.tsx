// @vitest-environment jsdom

import type { ComponentProps } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";
import { PostHogContext } from "posthog-js/react";
import type { PostHog } from "posthog-js";

import { Status } from "@/api/types";
import { BlockActionContext } from "@/store/BlockActionContext";
import {
  DebugStoreContext,
  DebugStoreProvider,
  type DebugStoreContextType,
} from "@/store/DebugStoreContext";

import { NodeHeader } from "./NodeHeader";

afterEach(() => {
  cleanup();
  queryClient.clear();
});

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

// isFeatureEnabled/onFeatureFlags are the only members useWorkflowStudioEnabled
// touches; a real PostHogProvider would fire network requests jsdom can't make.
const fakePostHogClient = {
  isFeatureEnabled: () => false,
  onFeatureFlags: () => () => {},
  featureFlags: { hasLoadedFlags: true },
} as unknown as PostHog;

const blockActionStub = {
  requestDeleteNodeCallback: () => {},
  duplicateNodeCallback: () => {},
  transmuteNodeCallback: () => {},
  toggleScriptForNodeCallback: () => {},
};

function renderNodeHeader(
  props: Partial<ComponentProps<typeof NodeHeader>>,
  // The Play control only mounts under debug mode / block runs, so a test that
  // needs it injects the store rather than driving the flag hooks.
  debugStore?: DebugStoreContextType,
  initialEntry = "/agents/wf-test/build",
) {
  const DebugWrapper = ({ children }: { children: React.ReactNode }) =>
    debugStore ? (
      <DebugStoreContext.Provider value={debugStore}>
        {children}
      </DebugStoreContext.Provider>
    ) : (
      <DebugStoreProvider>{children}</DebugStoreProvider>
    );
  return render(
    <QueryClientProvider client={queryClient}>
      <PostHogContext.Provider
        value={{ client: fakePostHogClient, bootstrap: undefined }}
      >
        <MemoryRouter initialEntries={[initialEntry]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/*"
              element={
                <ReactFlowProvider>
                  <BlockActionContext.Provider value={blockActionStub}>
                    <DebugWrapper>
                      <NodeHeader
                        blockLabel="block_1"
                        editable
                        nodeId="node-a"
                        totpIdentifier={null}
                        totpUrl={null}
                        type="code"
                        {...props}
                      />
                    </DebugWrapper>
                  </BlockActionContext.Provider>
                </ReactFlowProvider>
              }
            />
          </Routes>
        </MemoryRouter>
      </PostHogContext.Provider>
    </QueryClientProvider>,
  );
}

// jsdom has no layout engine: these tests can only pin the classes that
// carry the fix, not the actual squeeze/drift/clip behavior they prevent.
// That's verified against the real rendered component in a Chromium
// harness (see the PR description for the before/after screenshots).
describe("NodeHeader icon/title regressions (SKY-11885 / SKY-11887)", () => {
  test("icon wrapper has shrink-0 so a long title column can't compress it", () => {
    const { container } = renderNodeHeader({});
    const iconWrapper = container.querySelector(".border-border");
    expect(iconWrapper?.className).toContain("shrink-0");
  });

  test("does not apply a code-specific icon scale", () => {
    const { container } = renderNodeHeader({ type: "code" });
    const svg = container.querySelector(".border-border svg");
    expect(svg?.getAttribute("class") ?? "").not.toContain("scale-90");
  });

  test("display title carries no horizontal padding that would drift it from the subtitle", () => {
    renderNodeHeader({ blockLabel: "block_1" });
    const title = screen.getByText("block_1");
    expect(title.className).not.toContain("px-2");
  });

  test("edit-mode input offsets its padding via relative/left, not a margin that would shrink the auto-width column", () => {
    renderNodeHeader({ blockLabel: "block_1" });
    fireEvent.click(screen.getByText("block_1"));
    const input = screen.getByDisplayValue("block_1");
    expect(input.className).toContain("relative");
    expect(input.className).toContain("-left-1");
    expect(input.className).not.toMatch(/-mx-/);
  });
});

describe("NodeHeader block controls are named (SKY-12995)", () => {
  test("the ⋯ block-actions trigger is a real, labelled button rather than a bare icon", () => {
    renderNodeHeader({ blockLabel: "block_1" });
    const trigger = screen.getByRole("button", { name: "Block actions" });
    expect(trigger.tagName).toBe("BUTTON");
    expect(trigger.getAttribute("aria-haspopup")).toBe("menu");
  });

  // The click handler early-returns on the full inert set, so any inert state
  // the `disabled` attribute misses is a control that still takes focus and
  // announces as enabled while doing nothing (WCAG 4.1.2). No debug session
  // resolves in jsdom, which is one of those states.
  test("the Play control is disabled — not merely dimmed — while running the block would be a no-op", () => {
    renderNodeHeader(
      { blockLabel: "block_1" },
      { isDebugMode: true, blockRunsEnabled: false },
    );
    const play = screen.getByRole("button", { name: "Run this block" });
    expect((play as HTMLButtonElement).disabled).toBe(true);
  });
});

// The run-status query keeps serving its last payload after the targeted run
// changes or clears, which also disables it — so nothing refetches to correct a
// retained "running" and the block controls stay inert until a reload.
describe("NodeHeader block controls vs a retained run payload (SKY-15507)", () => {
  function blockActionsAreInert() {
    const trigger = screen.getByRole("button", { name: "Block actions" });
    return Boolean(trigger.closest(".pointer-events-none"));
  }

  test("a live run targeted by the URL still makes the block controls inert", () => {
    queryClient.setQueryData(["workflowRun", "wf-test", "wr_1"], {
      workflow_run_id: "wr_1",
      status: Status.Running,
    });
    renderNodeHeader(
      { blockLabel: "block_1" },
      undefined,
      "/agents/wf-test/build?wr=wr_1",
    );
    expect(blockActionsAreInert()).toBe(true);
  });

  test("a payload retained after the targeted run clears does not", () => {
    queryClient.setQueryData(["workflowRun", "wf-test", undefined], {
      workflow_run_id: "wr_1",
      status: Status.Running,
    });
    renderNodeHeader({ blockLabel: "block_1" });
    expect(blockActionsAreInert()).toBe(false);
  });
});
