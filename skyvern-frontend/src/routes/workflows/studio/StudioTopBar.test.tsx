// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  test,
  vi,
} from "vitest";

import { ProxyLocation, Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import {
  clearDeferredEdits,
  deferredEdits,
} from "@/hooks/useDeferredLockedEdit";

const { workflowRunQueryMock, saveWorkflowSpy, getClientMock, realRunQuery } =
  vi.hoisted(() => ({
    workflowRunQueryMock: vi.fn(),
    saveWorkflowSpy: vi.fn(() => Promise.resolve()),
    getClientMock: vi.fn(),
    // The identity cases drive the real query through a seeded cache; the rest
    // only need a payload, so they keep the cheaper stub.
    realRunQuery: { enabled: false },
  }));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import("../hooks/useWorkflowRunWithWorkflowQuery")
    >();
  return {
    useWorkflowRunWithWorkflowQuery: (
      options?: Parameters<typeof actual.useWorkflowRunWithWorkflowQuery>[0],
    ) =>
      realRunQuery.enabled
        ? actual.useWorkflowRunWithWorkflowQuery(options)
        : workflowRunQueryMock(),
  };
});
vi.mock("../editor/hooks/useSaveWorkflow", () => ({
  useSaveWorkflow: () => saveWorkflowSpy,
}));
vi.mock("@/api/AxiosClient", () => ({ getClient: getClientMock }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));

import { stringify as toYaml } from "yaml";

import type { BlockYAML } from "@/routes/workflows/types/workflowYamlTypes";
import { snapshotOf } from "@/routes/workflows/editor/workflowChangesSummary";
import { useRecordingStore } from "@/store/useRecordingStore";
import {
  SaveRefusedError,
  SaveStaleError,
  useWorkflowHasChangesStore,
  type WorkflowSaveData,
} from "@/store/WorkflowHasChangesStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowYamlEditorStore } from "@/store/WorkflowYamlEditorStore";

import { useAutoGenerateWorkflowTitle } from "../hooks/useAutoGenerateWorkflowTitle";
import { taskNodeDefaultData } from "../editor/nodes/TaskNode/types";

import { RunStopButton, SaveButton, TitleSection } from "./StudioTopBar";

// jsdom lacks ResizeObserver, which Radix's tooltip popper constructs once a
// tooltip opens. Install it for this suite only and restore afterward.
class MockResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeAll(() => {
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
});

afterAll(() => {
  vi.unstubAllGlobals();
});

function LocationProbe() {
  const location = useLocation();
  return (
    <>
      <div data-testid="location">
        {location.pathname}
        {location.search}
      </div>
      <div data-testid="location-state">
        {JSON.stringify(location.state ?? null)}
      </div>
    </>
  );
}

function renderAt(
  path: string,
  element = <RunStopButton />,
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  }),
) {
  return render(
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route
              path="/workflows/:workflowPermanentId/studio"
              element={element}
            />
            <Route
              path="/agents/:workflowPermanentId/run"
              element={<LocationProbe />}
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

function mockRun(status: Status, overrides: Record<string, unknown> = {}) {
  workflowRunQueryMock.mockReturnValue({
    data: {
      workflow_run_id: "wr_1",
      status,
      parameters: { query: "status report", payload: ["alpha"] },
      proxy_location: ProxyLocation.ResidentialDE,
      webhook_callback_url: "https://example.com/webhook",
      max_screenshot_scrolls: 8,
      run_with: "code",
      browser_profile_id: "profile_synthetic",
      task_v2: null,
      workflow: { deleted_at: null },
      ...overrides,
    },
  });
}

function locationState() {
  return JSON.parse(screen.getByTestId("location-state").textContent ?? "null");
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  realRunQuery.enabled = false;
});
beforeEach(() => mockRun(Status.Running));

describe("RunStopButton concurrency with a live block run", () => {
  test("a running block run keeps both Stop and Run available", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeNull();
  });

  test("starting a full run over a live block run asks for a soft confirm", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: /Run/ }));
    expect(screen.queryByText("Start a full run?")).not.toBeNull();
    expect(screen.queryByTestId("location")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Start full run" }));
    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("the confirm can be declined without navigating", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: /Run/ }));
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));

    expect(screen.queryByTestId("location")).toBeNull();
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
  });

  test("a running full run offers Stop only", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1");
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("an unavailable status replaces Stop with Run for its retained running payload", () => {
    workflowRunQueryMock.mockReturnValue({
      data: {
        workflow_run_id: "wr_1",
        status: Status.Running,
        task_v2: null,
        workflow: { deleted_at: null },
      },
      isError: true,
    });

    renderAt("/workflows/wpid_1/studio?wr=wr_1");
    expect(screen.queryByRole("button", { name: /Stop/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeNull();
  });

  test("a run payload retained after the focus clears does not hold Stop open", () => {
    workflowRunQueryMock.mockReturnValue({
      data: {
        workflow_run_id: "wr_1",
        status: Status.Running,
        task_v2: null,
        workflow: { deleted_at: null },
      },
      isError: false,
      isPlaceholderData: true,
    });

    renderAt("/workflows/wpid_1/studio");
    expect(screen.queryByRole("button", { name: /Stop/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeNull();
  });

  // An org switch re-keys the query, so the retained payload can carry the
  // focused run's own id while describing the org that was just left.
  test("a placeholder payload does not hold Stop open even when its id matches", () => {
    workflowRunQueryMock.mockReturnValue({
      data: {
        workflow_run_id: "wr_1",
        status: Status.Running,
        task_v2: null,
        workflow: { deleted_at: null },
      },
      isError: false,
      isPlaceholderData: true,
    });

    renderAt("/workflows/wpid_1/studio?wr=wr_1");
    expect(screen.queryByRole("button", { name: /Stop/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).not.toBeNull();
  });

  test("a finalized workflow run reruns with the legacy navigation state", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));

    expect(screen.queryByText("Start a full run?")).toBeNull();
    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toEqual({
      data: { query: "status report", payload: ["alpha"] },
      proxyLocation: ProxyLocation.ResidentialDE,
      webhookCallbackUrl: "https://example.com/webhook",
      maxScreenshotScrolls: 8,
      runWith: "code",
      browserProfileId: "profile_synthetic",
    });
  });

  test("no focused run starts fresh", () => {
    workflowRunQueryMock.mockReturnValue({ data: undefined });
    renderAt("/workflows/wpid_1/studio");

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("a finalized block run starts a fresh full run", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1&bl=Block%201");

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("a finalized task run starts fresh", () => {
    mockRun(Status.Failed, { task_v2: { task_id: "task_synthetic" } });
    renderAt("/workflows/wpid_1/studio?wr=wr_1");

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("an editor-open layout is NOT carried into the run form (full runs reset)", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1&panes=editor,copilot");

    fireEvent.click(screen.getByRole("button", { name: "Re-run" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
  });
});

// Global workflows can't start runs from the studio, but recipe pages run
// them in place — a live run must still be stoppable from the top bar.
describe("RunStopButton stopOnly (global workflows)", () => {
  test("a running run offers Stop and no Run", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("a running block run offers Stop only — no concurrent full run", () => {
    renderAt(
      "/workflows/wpid_1/studio?wr=wr_1&bl=Block%201",
      <RunStopButton stopOnly />,
    );
    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /Run/ })).toBeNull();
  });

  test("stopping asks for the same soft confirm", () => {
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);

    fireEvent.click(screen.getByRole("button", { name: /Stop/ }));
    expect(screen.queryByText("Stop this run?")).not.toBeNull();
  });

  test("a finished run renders nothing", () => {
    mockRun(Status.Completed);
    renderAt("/workflows/wpid_1/studio?wr=wr_1", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("no focused run renders nothing", () => {
    workflowRunQueryMock.mockReturnValue({ data: undefined });
    renderAt("/workflows/wpid_1/studio", <RunStopButton stopOnly />);
    expect(screen.queryByRole("button")).toBeNull();
  });
});

const block = (label: string, extra: Record<string, unknown> = {}): BlockYAML =>
  ({ label, block_type: "task", ...extra }) as unknown as BlockYAML;

const saveData = (blocks: Array<BlockYAML>): WorkflowSaveData =>
  ({
    title: "T",
    blocks,
    parameters: [],
    settings: { proxyLocation: "RESIDENTIAL" },
    workflow: {
      title: "T",
      workflow_definition: { version: 2, blocks: [], parameters: [] },
    },
  }) as unknown as WorkflowSaveData;

function renderSaveButton() {
  return render(
    <TooltipProvider delayDuration={0}>
      <SaveButton />
    </TooltipProvider>,
  );
}

// The confirmation is gated on live dirtiness computed in the click handler, not
// on the debounced canvas-only `contentDirty` dot — so a YAML edit or an
// edit-then-save inside the debounce window still shows the "Saving Changes"
// list. Each case here has `contentDirty` stale-false to exercise that gap.
describe("SaveButton confirmation gating", () => {
  afterEach(() => {
    useWorkflowSnapshotStore.getState().clearSnapshot();
    useWorkflowHasChangesStore.setState({
      getSaveData: () => null,
      saveIsPending: false,
      saveBlockedReason: null,
    });
    useWorkflowYamlEditorStore.setState({
      active: false,
      draft: "",
      entrySnapshot: "",
    });
    useRecordingStore.setState({ isRecording: false });
  });

  test("confirms a dirty draft even when contentDirty is stale-false", () => {
    const clean = saveData([block("a", { url: "x" })]);
    const dirty = saveData([block("a", { url: "y" })]);
    useWorkflowHasChangesStore.setState({
      getSaveData: () => dirty,
      saveIsPending: false,
    });
    useWorkflowSnapshotStore.setState({
      snapshot: snapshotOf(clean),
      contentDirty: false,
      userHasEdited: false,
    });

    renderSaveButton();
    fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));

    expect(screen.queryByText("Saving Changes")).not.toBeNull();
    expect(saveWorkflowSpy).not.toHaveBeenCalled();
  });

  test.each([new SaveRefusedError(), new SaveStaleError()])(
    "closes the save dialog after %s",
    async (error) => {
      const clean = saveData([block("a", { url: "x" })]);
      const dirty = saveData([block("a", { url: "y" })]);
      useWorkflowHasChangesStore.setState({
        getSaveData: () => dirty,
        saveIsPending: false,
        hasChanges: true,
      });
      useWorkflowSnapshotStore.setState({
        snapshot: snapshotOf(clean),
        contentDirty: false,
        userHasEdited: false,
      });

      renderSaveButton();
      fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));

      expect(screen.queryByText("Saving Changes")).not.toBeNull();
      saveWorkflowSpy.mockRejectedValueOnce(error);
      fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
      await waitFor(() =>
        expect(screen.queryByText("Saving Changes")).toBeNull(),
      );
      expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    },
  );

  test("saves directly with no confirmation when the draft matches the baseline", () => {
    const clean = saveData([block("a", { url: "x" })]);
    useWorkflowHasChangesStore.setState({
      getSaveData: () => clean,
      saveIsPending: false,
    });
    useWorkflowSnapshotStore.setState({
      snapshot: snapshotOf(clean),
      contentDirty: false,
      userHasEdited: false,
    });

    renderSaveButton();
    fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));

    expect(screen.queryByText("Saving Changes")).toBeNull();
    expect(saveWorkflowSpy).toHaveBeenCalledTimes(1);
  });

  test("confirms an uncommitted YAML-draft edit the canvas hasn't caught up to", () => {
    const canvas = saveData([block("a", { block_type: "code", code: "# a" })]);
    // Baseline and canvas agree; the edit lives only in the YAML draft.
    useWorkflowHasChangesStore.setState({
      getSaveData: () => canvas,
      saveIsPending: false,
    });
    useWorkflowSnapshotStore.setState({
      snapshot: snapshotOf(canvas),
      contentDirty: false,
      userHasEdited: false,
    });
    useWorkflowYamlEditorStore.setState({
      active: true,
      entrySnapshot: toYaml({ parameters: [], blocks: canvas.blocks }),
      draft: toYaml({
        parameters: [],
        blocks: [
          ...canvas.blocks,
          { label: "b", block_type: "code", code: "# b" },
        ],
      }),
    });

    renderSaveButton();
    fireEvent.click(screen.getByRole("button", { name: "Save workflow" }));

    expect(screen.queryByText("Saving Changes")).not.toBeNull();
    expect(saveWorkflowSpy).not.toHaveBeenCalled();
  });

  // A hold used to disable Save outright, which left the reason in a tooltip on a dead control
  // and the only way out (reload) named nowhere the user was looking.
  test("keeps Save live under a hold and explains it in the confirmation instead", () => {
    const clean = saveData([block("a", { url: "x" })]);
    useWorkflowHasChangesStore.setState({
      getSaveData: () => clean,
      saveIsPending: false,
      saveBlockedReason:
        "This workflow changed after Copilot staged its proposal.",
    });
    useWorkflowSnapshotStore.setState({
      snapshot: snapshotOf(clean),
      contentDirty: false,
      userHasEdited: false,
    });

    renderSaveButton();
    const save = screen.getByRole("button", {
      name: "Save workflow (paused): This workflow changed after Copilot staged its proposal.",
    });
    expect(save.matches(":disabled")).toBe(false);
    fireEvent.click(save);

    expect(screen.queryByText("Save is paused")).not.toBeNull();
    expect(
      screen.queryByText(
        "This workflow changed after Copilot staged its proposal.",
      ),
    ).not.toBeNull();
    expect(
      screen.queryByRole("button", { name: "Reload and discard my edits" }),
    ).not.toBeNull();
    // The save path refuses while a hold is set, so the dialog offers no save for it to refuse.
    expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save anyway" })).toBeNull();
    expect(saveWorkflowSpy).not.toHaveBeenCalled();
  });
});

describe("TitleSection title link + edit affordance", () => {
  beforeEach(() => {
    clearDeferredEdits();
    useWorkflowYamlEditorStore.setState({
      commitInProgress: false,
      copilotAcceptance: null,
    });
    useWorkflowTitleStore.setState({ title: "My Workflow" });
    useWorkflowHasChangesStore.setState({ hasChanges: false });
    useRecordingStore.setState({ isRecording: false });
  });

  function AutoTitleGenerator() {
    useAutoGenerateWorkflowTitle(
      [
        {
          id: "task-title",
          type: "task",
          position: { x: 0, y: 0 },
          data: {
            ...taskNodeDefaultData,
            label: "task",
            url: "https://example.com",
          },
        },
      ],
      [],
    );
    return null;
  }

  function renderTitleSection(editable = true, generateTitle = false) {
    return render(
      <TooltipProvider delayDuration={0}>
        <MemoryRouter initialEntries={["/agents/wpid_abc/studio"]}>
          <Routes>
            <Route
              path="/agents/:workflowPermanentId/studio"
              element={
                <>
                  {generateTitle && <AutoTitleGenerator />}
                  <TitleSection editable={editable} />
                </>
              }
            />
          </Routes>
        </MemoryRouter>
      </TooltipProvider>,
    );
  }

  test("renders the title as a link to the workflow runs page", () => {
    renderTitleSection();
    const link = screen.getByRole("link", { name: "My Workflow" });
    expect(link.getAttribute("href")).toBe("/agents/wpid_abc/runs");
  });

  test("cues the title as a link and tooltips its runs destination", async () => {
    renderTitleSection();
    const link = screen.getByRole("link", { name: "My Workflow" });
    expect(link.getAttribute("href")).toBe("/agents/wpid_abc/runs");
    // jsdom can't evaluate :hover; pin the color cue (mirrors WorkflowScriptsPage's runs link).
    expect(link.className).toContain("hover:text-blue-700");
    expect(link.className).toContain("dark:hover:text-blue-400");
    // Radix mounts the tooltip content on focus (visible + a11y copies), so
    // assert the destination label appears rather than a single node.
    fireEvent.focusIn(link);
    expect(
      (await screen.findAllByText("View past runs")).length,
    ).toBeGreaterThan(0);
  });

  test("enters the shared rename input from the edit button and commits into the title + dirty stores", () => {
    renderTitleSection();

    fireEvent.click(
      screen.getByRole("button", { name: "Click to edit title" }),
    );

    const input = screen.getByDisplayValue("My Workflow");
    fireEvent.change(input, { target: { value: "Renamed WF" } });
    fireEvent.blur(input);

    expect(useWorkflowTitleStore.getState().title).toBe("Renamed WF");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });

  test.each([
    { commitInProgress: true, copilotAcceptance: null },
    { commitInProgress: false, copilotAcceptance: Symbol("acceptance") },
  ])("preserves a rename entered before the editor locks: %s", (lock) => {
    renderTitleSection();
    fireEvent.click(
      screen.getByRole("button", { name: "Click to edit title" }),
    );
    const input = screen.getByDisplayValue("My Workflow");
    fireEvent.change(input, { target: { value: "Renamed during save" } });

    act(() => useWorkflowYamlEditorStore.setState(lock));
    fireEvent.blur(input);
    expect(useWorkflowTitleStore.getState().title).toBe("My Workflow");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
    expect(
      screen.queryByRole("button", { name: "Click to edit title" }),
    ).toBeNull();

    act(() =>
      useWorkflowYamlEditorStore.setState({
        commitInProgress: false,
        copilotAcceptance: null,
      }),
    );
    expect(useWorkflowTitleStore.getState().title).toBe("Renamed during save");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
  });

  test.each(["My Workflow", "Accepted title"])(
    "resolves a held rename once after remount with store title %s",
    (storeTitle) => {
      const view = renderTitleSection();
      fireEvent.click(
        screen.getByRole("button", { name: "Click to edit title" }),
      );
      fireEvent.change(screen.getByDisplayValue("My Workflow"), {
        target: { value: "Held rename" },
      });
      act(() =>
        useWorkflowYamlEditorStore.setState({
          copilotAcceptance: Symbol("queued-send"),
        }),
      );
      view.unmount();
      expect(deferredEdits.get("wpid_abc:title")?.value).toBe("Held rename");
      act(() =>
        useWorkflowTitleStore
          .getState()
          .setTitle(storeTitle, { fromYamlCommit: true }),
      );
      const originalSetTitle = useWorkflowTitleStore.getState().setTitle;
      const setTitle = vi.fn(originalSetTitle);
      useWorkflowTitleStore.setState({ setTitle });
      try {
        renderTitleSection();
        expect(useWorkflowTitleStore.getState().title).toBe(storeTitle);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
        act(() =>
          useWorkflowYamlEditorStore.setState({ copilotAcceptance: null }),
        );
        const applies = storeTitle === "My Workflow";
        expect(useWorkflowTitleStore.getState().title).toBe(
          applies ? "Held rename" : storeTitle,
        );
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(applies);
        expect(setTitle).toHaveBeenCalledTimes(applies ? 1 : 0);
        expect(deferredEdits.has("wpid_abc:title")).toBe(false);
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: true }),
        );
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: false }),
        );
        expect(setTitle).toHaveBeenCalledTimes(applies ? 1 : 0);
      } finally {
        useWorkflowTitleStore.setState({ setTitle: originalSetTitle });
      }
    },
  );

  test("keeps a newer store title when the deferred rename conflicts", () => {
    renderTitleSection();
    fireEvent.click(
      screen.getByRole("button", { name: "Click to edit title" }),
    );
    fireEvent.change(screen.getByDisplayValue("My Workflow"), {
      target: { value: "Deferred rename" },
    });
    act(() => useWorkflowYamlEditorStore.setState({ commitInProgress: true }));
    act(() => {
      useWorkflowTitleStore
        .getState()
        .setTitle("Accepted title", { fromYamlCommit: true });
      useWorkflowYamlEditorStore.setState({ commitInProgress: false });
    });
    expect(useWorkflowTitleStore.getState().title).toBe("Accepted title");
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(false);
  });

  test.each(["User title", "New Workflow"])(
    "applies pending user rename %s before a pending generated title",
    async (userTitle) => {
      vi.useFakeTimers();
      try {
        useWorkflowTitleStore.setState({
          title: "New Agent",
          titleHasBeenGenerated: false,
        });
        let resolveTitle!: (value: { data: { title: string } }) => void;
        const response = new Promise<{ data: { title: string } }>((resolve) => {
          resolveTitle = resolve;
        });
        const post = vi.fn(() => response);
        getClientMock.mockResolvedValue({ post });
        renderTitleSection(true, true);
        await act(async () => {
          await vi.advanceTimersByTimeAsync(4000);
        });
        expect(post).toHaveBeenCalledTimes(1);
        fireEvent.click(
          screen.getByRole("button", { name: "Click to edit title" }),
        );
        fireEvent.change(screen.getByDisplayValue("New Agent"), {
          target: { value: userTitle },
        });
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: true }),
        );
        await act(async () => {
          resolveTitle({ data: { title: "Generated title" } });
        });
        expect(useWorkflowTitleStore.getState().title).toBe("New Agent");
        act(() =>
          useWorkflowYamlEditorStore.setState({ commitInProgress: false }),
        );
        expect(useWorkflowTitleStore.getState().title).toBe(userTitle);
        expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
      } finally {
        vi.useRealTimers();
      }
    },
  );

  test("keeps the runs link but hides the edit button when not editable", () => {
    renderTitleSection(false);
    expect(screen.getByRole("link", { name: "My Workflow" })).toBeTruthy();
    expect(
      screen.queryByRole("button", { name: "Click to edit title" }),
    ).toBeNull();
  });

  test("renders the edit pencil visibly at rest with a borderless hover-bg affordance", () => {
    renderTitleSection();
    const button = screen.getByRole("button", { name: "Click to edit title" });
    // Always visible (no opacity reveal), soft hover background, no border/outline.
    expect(button.className).not.toContain("opacity-0");
    expect(button.className).toContain("hover:bg-slate-500/20");
    expect(button.className).not.toContain("border");
  });
});

// Seeds the payload a run switch leaves behind: the response cached under the key
// of the run now being requested, which is what keepPreviousData hands over.
function seedRetained(
  client: QueryClient,
  requestedId: string,
  runId: string,
  status: Status,
) {
  client.setQueryData(["workflowRun", requestedId], {
    workflow_run_id: runId,
    status,
    parameters: { query: "status report" },
    task_v2: null,
    workflow: { deleted_at: null },
  });
}

function renderRealRunQueryAt(
  path: string,
  seed: (client: QueryClient) => void,
) {
  realRunQuery.enabled = true;
  getClientMock.mockResolvedValue({ get: () => new Promise(() => {}) });
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  seed(client);
  return renderAt(path, <RunStopButton />, client);
}

describe("RunStopButton against a retained run payload", () => {
  test("a run other than the one in view leaves no Stop and no rerun to seed", () => {
    renderRealRunQueryAt("/workflows/wpid_1/studio?wr=wr_1", (client) =>
      seedRetained(client, "wr_1", "wr_previous", Status.Running),
    );

    expect(screen.queryByRole("button", { name: /Stop/ })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    expect(screen.getByTestId("location").textContent).toBe(
      "/agents/wpid_1/run",
    );
    expect(locationState()).toBeNull();
  });

  test("the run actually in view still offers Stop", () => {
    renderRealRunQueryAt("/workflows/wpid_1/studio?wr=wr_1", (client) =>
      seedRetained(client, "wr_1", "wr_1", Status.Running),
    );

    expect(screen.queryByRole("button", { name: /Stop/ })).not.toBeNull();
  });
});
