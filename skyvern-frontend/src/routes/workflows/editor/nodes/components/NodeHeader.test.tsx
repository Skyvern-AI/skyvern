// @vitest-environment jsdom

import type { ComponentProps } from "react";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ReactFlowProvider, useNodes } from "@xyflow/react";
import { PostHogContext } from "posthog-js/react";
import type { PostHog } from "posthog-js";

import { Status } from "@/api/types";
import { toast } from "@/components/ui/use-toast";
import {
  beginSaveTransaction,
  createYamlCommitOwner,
  finishSaveTransaction,
  registerEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";
import { BlockActionContext } from "@/store/BlockActionContext";
import {
  DebugStoreContext,
  DebugStoreProvider,
  type DebugStoreContextType,
} from "@/store/DebugStoreContext";

import { NodeHeader } from "./NodeHeader";
import { WorkflowScopeContext } from "../../WorkflowScopeContext";
import type { AppNode } from "..";
import { codeBlockNodeDefaultData } from "../CodeBlockNode/types";
import {
  makeCollapseKey,
  useNodeCollapseStore,
} from "../../collapse/useNodeCollapseStore";

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

afterEach(() => {
  cleanup();
  queryClient.clear();
  useWorkflowYamlEditorStore.setState(
    useWorkflowYamlEditorStore.getInitialState(),
  );
  useNodeCollapseStore.setState({ collapsed: {} });
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
  initialNodes: AppNode[] = [],
  scopeId: string | null = null,
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
                <ReactFlowProvider defaultNodes={initialNodes}>
                  <GraphLabels />
                  <BlockActionContext.Provider value={blockActionStub}>
                    <DebugWrapper>
                      <WorkflowScopeContext.Provider
                        value={{ workflowId: scopeId, readOnly: false }}
                      >
                        <NodeHeader
                          blockLabel="block_1"
                          editable
                          nodeId="node-a"
                          totpIdentifier={null}
                          totpUrl={null}
                          type="code"
                          {...props}
                        />
                      </WorkflowScopeContext.Provider>
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

function GraphLabels() {
  const nodes = useNodes<AppNode>();
  return (
    <output data-testid="graph-labels">
      {nodes.map((node) => node.data.label).join(",")}
    </output>
  );
}

test("holds a label commit during a save and applies it on release", async () => {
  const node: AppNode = {
    id: "node-a",
    type: "codeBlock",
    position: { x: 0, y: 0 },
    data: { ...codeBlockNodeDefaultData, label: "block_1" },
  };
  useNodeCollapseStore.getState().collapseAll("__global__", ["block_1"]);
  const collapsed = useNodeCollapseStore.getState().collapsed;
  renderNodeHeader({}, undefined, undefined, [node]);
  fireEvent.click(screen.getByText("block_1", { selector: "h1" }));
  const input = screen.getByDisplayValue("block_1");
  fireEvent.change(input, { target: { value: "renamed block" } });
  const owner = createYamlCommitOwner("wf-test");
  act(() => {
    registerEditorOwner(owner);
    expect(beginSaveTransaction(owner)).toBe(true);
  });
  vi.mocked(toast).mockClear();
  await act(async () => fireEvent.blur(input));
  expect(screen.getByText("block_1", { selector: "h1" })).toBeTruthy();
  expect(screen.getByTestId("graph-labels").textContent).toBe("block_1");
  expect(useNodeCollapseStore.getState().collapsed).toEqual(collapsed);
  expect(toast).not.toHaveBeenCalled();
  await act(async () => finishSaveTransaction(owner));
  expect(screen.getByTestId("graph-labels").textContent).toBe("renamed_block");
  expect(
    useNodeCollapseStore.getState().collapsed[
      makeCollapseKey("__global__", "block_1")
    ],
  ).toBeUndefined();
  expect(
    useNodeCollapseStore.getState().collapsed[
      makeCollapseKey("__global__", "renamed_block")
    ],
  ).toBe(true);
  expect(toast).toHaveBeenCalledExactlyOnceWith({
    title: "Block label adjusted",
    description:
      "Block labels can only contain letters, numbers, and underscores. Invalid characters have been replaced.",
  });
});

test.each(["conditional", "for_loop", "while_loop"] as const)(
  "%s collapse stays unchanged during a save and works after release",
  (type) => {
    renderNodeHeader({ type }, undefined, undefined, [], "wf-test");
    const button = screen.getByRole("button", {
      name: "Collapse block",
    }) as HTMLButtonElement;
    const owner = createYamlCommitOwner("wf-test");
    act(() => {
      registerEditorOwner(owner);
      expect(beginSaveTransaction(owner)).toBe(true);
    });
    const collapsed = useNodeCollapseStore.getState().collapsed;
    fireEvent.click(button);
    expect(useNodeCollapseStore.getState().collapsed).toEqual(collapsed);
    expect(button.disabled).toBe(true);
    act(() => finishSaveTransaction(owner));
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(button);
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(
      useNodeCollapseStore.getState().collapsed[
        makeCollapseKey("wf-test", "block_1")
      ],
    ).toBe(true);
  },
);

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

test("a paused block run leaves per-block controls idle", () => {
  queryClient.setQueryData(["workflowRun", "wf-test", "wr_paused"], {
    workflow_run_id: "wr_paused",
    status: Status.Paused,
  });
  renderNodeHeader(
    { blockLabel: "block_1" },
    { isDebugMode: true, blockRunsEnabled: false },
    "/agents/wf-test/build?wr=wr_paused&bl=block_1",
  );
  expect(
    screen
      .getByRole("button", { name: "Block actions" })
      .closest(".pointer-events-none"),
  ).toBeNull();
  expect(screen.getByRole("button", { name: "Run this block" })).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Stop this block" })).toBeNull();
});
