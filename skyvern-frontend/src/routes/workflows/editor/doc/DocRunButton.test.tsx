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
import { AxiosError } from "axios";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Status } from "@/api/types";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { WorkflowRunBlock } from "../../types/workflowRunTypes";

const { toastSpy, docRunState, saveSpy } = vi.hoisted(() => ({
  toastSpy: vi.fn(),
  docRunState: { current: {} as Record<string, unknown> },
  saveSpy: vi.fn(),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: toastSpy }));
vi.mock("@/api/AxiosClient", () => ({ getClient: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(),
}));
vi.mock("./useDocRun", () => ({ useDocRun: () => docRunState.current }));
vi.mock("../hooks/useSaveWorkflow", () => ({ useSaveWorkflow: () => vi.fn() }));
vi.mock("@/store/WorkflowHasChangesStore", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  useWorkflowSave: () => ({ mutateAsync: saveSpy }),
}));

import { getClient } from "@/api/AxiosClient";
import {
  SaveRefusedError,
  useWorkflowHasChangesStore,
} from "@/store/WorkflowHasChangesStore";
import {
  selectEditorMutationLocked,
  useWorkflowYamlEditorStore,
  type YamlCommitOwner,
} from "@/store/WorkflowYamlEditorStore";
import { DocEditBar } from "./DocEditing";
import { DocRunButton } from "./DocRunButton";
import { useDocRunStore } from "./docRunStore";
import { useDocSaveFailureStore } from "./docSaveState";

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

describe("DocRunButton pre-save", () => {
  const owner: YamlCommitOwner = {
    workflowPermanentId: "wpid_1",
    active: true,
  };
  const lockedBy = (lockOwner: YamlCommitOwner) => ({
    owner: lockOwner,
    kind: "save" as const,
    persisting: true,
  });
  const unanswered = () => new AxiosError("Network Error", "ERR_NETWORK");

  beforeEach(() => {
    toastSpy.mockClear();
    vi.mocked(getClient).mockClear();
    saveSpy.mockReset();
    useWorkflowHasChangesStore.setState({ hasChanges: true });
    useDocRunStore.setState({ stepCount: 1, inputs: [] });
    docRunState.current = {
      runId: null,
      run: undefined,
      blocksByLabel: new Map(),
      loopsByLabel: new Map(),
      live: false,
      owned: false,
      blockRun: false,
    };
  });
  afterEach(() => {
    cleanup();
    useWorkflowHasChangesStore.setState({ hasChanges: false });
    useWorkflowYamlEditorStore.setState({
      editorOwner: null,
      pendingSaves: {},
      commitOwner: null,
      commitInProgress: false,
      lockKind: null,
    });
    useDocSaveFailureStore.setState({ failures: {} });
  });

  async function pressRun(withBar = false) {
    const client = new QueryClient();
    render(
      <QueryClientProvider client={client}>
        <TooltipProvider>
          <MemoryRouter initialEntries={["/workflows/wpid_1/studio"]}>
            <Routes>
              <Route
                path="/workflows/:workflowPermanentId/studio"
                element={
                  <>
                    {withBar ? <DocEditBar /> : null}
                    <DocRunButton />
                  </>
                }
              />
            </Routes>
          </MemoryRouter>
        </TooltipProvider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    const dialog = await screen.findByRole("dialog", { name: "Run with" });
    fireEvent.submit(dialog.querySelector("form")!);
  }

  it("a Run whose save gets no answer leaves the Doc editable, says why, and does not start", async () => {
    useWorkflowYamlEditorStore.setState({
      editorOwner: owner,
      pendingSaves: { wpid_1: lockedBy(owner) },
      commitOwner: owner,
      commitInProgress: true,
      lockKind: "save",
    });
    sessionStorage.setItem("workflow-pending-save:wpid_1", "{}");
    saveSpy.mockRejectedValueOnce(unanswered());

    await pressRun(true);

    expect(
      await screen.findByText(/Couldn't save: can't reach the server/),
    ).toBeTruthy();
    expect(screen.getByRole("button", { name: "Try again" })).toBeTruthy();
    const state = useWorkflowYamlEditorStore.getState();
    expect(state.pendingSaves.wpid_1).toBeUndefined();
    expect(selectEditorMutationLocked(state)).toBe(false);
    expect(sessionStorage.getItem("workflow-pending-save:wpid_1")).toBe(null);
    expect(toastSpy).toHaveBeenCalledTimes(1);
    expect(toastSpy.mock.calls[0]![0]).toMatchObject({
      variant: "destructive",
      title: "Couldn't save: can't reach the server",
    });
    expect(getClient).not.toHaveBeenCalled();
  });

  it("a Run's failed save releases its own workflow's lock, not the one now open", async () => {
    const b: YamlCommitOwner = { workflowPermanentId: "wpid_b", active: true };
    useWorkflowYamlEditorStore.setState({
      editorOwner: owner,
      pendingSaves: { wpid_1: lockedBy(owner), wpid_b: lockedBy(b) },
    });
    saveSpy.mockImplementationOnce(async () => {
      useWorkflowYamlEditorStore.setState({ editorOwner: b });
      throw unanswered();
    });

    await pressRun();

    await waitFor(() =>
      expect(
        useWorkflowYamlEditorStore.getState().pendingSaves.wpid_1,
      ).toBeUndefined(),
    );
    expect(
      useWorkflowYamlEditorStore.getState().pendingSaves.wpid_b?.persisting,
    ).toBe(true);
    expect(useDocSaveFailureStore.getState().failures).toEqual({
      wpid_1: "can't reach the server",
    });
  });

  it("a Run whose save is refused stores nothing and leaves the notice to the save", async () => {
    useWorkflowYamlEditorStore.setState({ editorOwner: owner });
    saveSpy.mockRejectedValueOnce(
      new SaveRefusedError("Resolve the Copilot proposal before saving."),
    );

    await pressRun(true);

    await waitFor(() => expect(saveSpy).toHaveBeenCalled());
    await act(async () => {});
    expect(useDocSaveFailureStore.getState().failures).toEqual({});
    expect(screen.queryByText(/Couldn't save/)).toBe(null);
    expect(toastSpy).not.toHaveBeenCalled();
    expect(getClient).not.toHaveBeenCalled();
  });

  it("a Run whose save the server rejects records the reason and adds no toast", async () => {
    useWorkflowYamlEditorStore.setState({ editorOwner: owner });
    saveSpy.mockRejectedValueOnce(
      new AxiosError("Server Error", "ERR_BAD_RESPONSE", undefined, undefined, {
        status: 500,
        data: {},
      } as never),
    );

    await pressRun();

    await waitFor(() =>
      expect(useDocSaveFailureStore.getState().failures).toEqual({
        wpid_1: "the server had a problem",
      }),
    );
    expect(toastSpy).not.toHaveBeenCalled();
    expect(getClient).not.toHaveBeenCalled();
  });

  it("a new Run clears the earlier failure when its save starts", async () => {
    useWorkflowYamlEditorStore.setState({ editorOwner: owner });
    useDocSaveFailureStore.setState({
      failures: { wpid_1: "can't reach the server" },
    });
    saveSpy.mockReturnValueOnce(new Promise(() => {}));

    await pressRun();

    await waitFor(() => expect(saveSpy).toHaveBeenCalled());
    expect(useDocSaveFailureStore.getState().failures).toEqual({});
  });
});
