// @vitest-environment jsdom

import type { ComponentProps } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ReactFlowProvider } from "@xyflow/react";
import { PostHogContext } from "posthog-js/react";
import type { PostHog } from "posthog-js";

import { BlockActionContext } from "@/store/BlockActionContext";
import { DebugStoreProvider } from "@/store/DebugStoreContext";

import { NodeHeader } from "./NodeHeader";

afterEach(() => {
  cleanup();
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

function renderNodeHeader(props: Partial<ComponentProps<typeof NodeHeader>>) {
  return render(
    <QueryClientProvider client={queryClient}>
      <PostHogContext.Provider
        value={{ client: fakePostHogClient, bootstrap: undefined }}
      >
        <MemoryRouter initialEntries={["/agents/wf-test/build"]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/*"
              element={
                <ReactFlowProvider>
                  <BlockActionContext.Provider value={blockActionStub}>
                    <DebugStoreProvider>
                      <NodeHeader
                        blockLabel="block_1"
                        editable
                        nodeId="node-a"
                        totpIdentifier={null}
                        totpUrl={null}
                        type="code"
                        {...props}
                      />
                    </DebugStoreProvider>
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
    const iconWrapper = container.querySelector(".border-slate-600");
    expect(iconWrapper?.className).toContain("shrink-0");
  });

  test("does not apply a code-specific icon scale", () => {
    const { container } = renderNodeHeader({ type: "code" });
    const svg = container.querySelector(".border-slate-600 svg");
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
