// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSettingsStore } from "@/store/SettingsStore";
import { setRecordBrowserContext } from "@/util/recordBrowserTelemetry";
import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
} from "@/store/WorkflowHasChangesStore";
import {
  createYamlCommitOwner,
  registerEditorOwner,
  unregisterEditorOwner,
  useWorkflowYamlEditorStore,
} from "@/store/WorkflowYamlEditorStore";

import { PendingRecordingStartDialog } from "./PendingRecordingStartDialog";
import {
  requestRecordingStart,
  usePendingRecordingStartGate,
} from "./pendingRecordingStartGate";

const capture = vi.hoisted(() => vi.fn());
const deleteRecording = vi.hoisted(() => vi.fn());
vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ delete: deleteRecording })),
}));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => null,
}));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/components/ui/use-toast", () => ({ toast }));
vi.mock("posthog-js", () => ({ default: { capture } }));

function setPending(workflowPermanentId = "wpid_test") {
  useWorkflowHasChangesStore.setState({
    hasChanges: true,
    pendingRecordingId: "br-pending",
    pendingRecordingWorkflowPermanentId: workflowPermanentId,
  });
}

function clearChanges() {
  useWorkflowHasChangesStore.setState({
    hasChanges: false,
    pendingRecordingId: null,
    pendingRecordingWorkflowPermanentId: null,
  });
}

function renderDialog(
  onSave: () => Promise<void> = async () => clearChanges(),
  onDiscard: () => void = clearChanges,
) {
  render(<PendingRecordingStartDialog onSave={onSave} onDiscard={onDiscard} />);
}

const blockedEvents = () =>
  capture.mock.calls.filter(
    ([event]) => event === "record_browser.start_blocked_pending_changes",
  );

describe("requestRecordingStart", () => {
  beforeEach(() => {
    capture.mockClear();
    deleteRecording.mockReset().mockResolvedValue({});
    toast.mockClear();
    useSettingsStore.getState().setBrowserSessionId(null);
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    usePendingRecordingStartGate.setState({
      blockedStart: null,
      awaitingConfirmedSave: false,
    });
  });

  afterEach(cleanup);

  it("starts immediately when nothing is pending", () => {
    const start = vi.fn();
    requestRecordingStart(start, "launcher");
    expect(start).toHaveBeenCalledOnce();
    expect(blockedEvents()).toHaveLength(0);
  });

  it("holds the start behind the prompt and counts the attempt once", () => {
    setPending();
    renderDialog();
    const start = vi.fn();
    setRecordBrowserContext({ recording_attempt_id: "previous-attempt" });
    act(() => requestRecordingStart(start, "auto_record"));
    expect(start).not.toHaveBeenCalled();
    expect(
      screen.getByText("Save or discard changes before recording"),
    ).toBeTruthy();
    expect(screen.getByText(/This can't be undone/)).toBeTruthy();
    expect(blockedEvents()).toEqual([
      [
        "record_browser.start_blocked_pending_changes",
        expect.objectContaining({
          entry_point: "auto_record",
          workflow_permanent_id: "wpid_test",
          recording_attempt_id: undefined,
        }),
      ],
    ]);
  });

  it.each([
    ["Save and record", "save"],
    ["Discard and record", "discard"],
  ])("%s resolves the pending change, then starts", async (label) => {
    setPending();
    const onSave = vi.fn(async () => clearChanges());
    const onDiscard = vi.fn(clearChanges);
    renderDialog(onSave, onDiscard);
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () => fireEvent.click(screen.getByText(label)));
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(null);
    expect(start).toHaveBeenCalledOnce();
    expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
  });

  it.each(["Escape", "Cancel"])(
    "%s keeps the changes and does not start",
    (how) => {
      setPending();
      renderDialog();
      const start = vi.fn();
      act(() =>
        requestRecordingStart(start, "node_adder", {
          isStillValid: () => true,
          recordsAfterDiscard: false,
        }),
      );
      if (how === "Cancel") fireEvent.click(screen.getByText("Cancel"));
      else
        fireEvent.keyDown(document.activeElement ?? document.body, {
          key: "Escape",
        });
      expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
      expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
        "br-pending",
      );
      expect(start).not.toHaveBeenCalled();
    },
  );

  it("does not start when the save fails", async () => {
    setPending();
    renderDialog(async () => {
      throw new Error("save failed");
    });
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () => fireEvent.click(screen.getByText("Save and record")));
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
      "br-pending",
    );
    expect(start).not.toHaveBeenCalled();
  });

  it("resumes Save and record after the code-cache confirmation saves", async () => {
    setPending();
    renderDialog(async () => {
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(true);
      throw new Error("needs code cache confirmation");
    });
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () => fireEvent.click(screen.getByText("Save and record")));
    expect(start).not.toHaveBeenCalled();
    expect(
      screen.queryByText("Save or discard changes before recording"),
    ).toBeNull();
    act(() => {
      clearChanges();
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(false);
    });
    expect(start).toHaveBeenCalledOnce();
  });

  it("keeps Save and record when the confirmation closes while its save is running", async () => {
    setPending();
    renderDialog(async () => {
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(true);
      throw new Error("needs code cache confirmation");
    });
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () => fireEvent.click(screen.getByText("Save and record")));
    // Yes was clicked; its save holds the editor when the dialog is dismissed.
    act(() => {
      useWorkflowYamlEditorStore.setState({ commitInProgress: true });
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(false);
    });
    expect(usePendingRecordingStartGate.getState().blockedStart).not.toBeNull();
    act(() => {
      clearChanges();
      useWorkflowYamlEditorStore.setState({ commitInProgress: false });
    });
    expect(start).toHaveBeenCalledOnce();
  });

  it("does not record when the code-cache confirmation is cancelled", async () => {
    setPending();
    renderDialog(async () => {
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(true);
      throw new Error("needs code cache confirmation");
    });
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () => fireEvent.click(screen.getByText("Save and record")));
    act(() =>
      useWorkflowHasChangesStore
        .getState()
        .setShowConfirmCodeCacheDeletion(false),
    );
    act(clearChanges);
    expect(start).not.toHaveBeenCalled();
    expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
  });

  it.each([
    [
      "the editor changed",
      () => {
        const owner = useWorkflowYamlEditorStore.getState().editorOwner!;
        unregisterEditorOwner(owner);
        registerEditorOwner(createYamlCommitOwner("wpid_other"));
      },
    ],
    [
      "the browser session changed",
      () => useSettingsStore.getState().setBrowserSessionId("pbs_other"),
    ],
  ])("does not start after the save when %s", async (_, change) => {
    useSettingsStore.getState().setBrowserSessionId("pbs_test");
    setPending();
    let finishSave = () => {};
    renderDialog(
      () =>
        new Promise<void>((resolve) => {
          finishSave = () => {
            clearChanges();
            resolve();
          };
        }),
    );
    const start = vi.fn();
    act(() => requestRecordingStart(start, "edge"));
    act(() => {
      fireEvent.click(screen.getByText("Save and record"));
    });
    act(change);
    await act(async () => finishSave());
    expect(start).not.toHaveBeenCalled();
    expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
  });

  it.each(["edge", "node_adder"] as const)(
    "from a canvas %s, Discard changes discards without recording",
    async (entryPoint) => {
      setPending();
      const onDiscard = vi.fn(clearChanges);
      renderDialog(undefined, onDiscard);
      const start = vi.fn();
      act(() =>
        requestRecordingStart(start, entryPoint, {
          isStillValid: () => true,
          recordsAfterDiscard: false,
        }),
      );
      expect(screen.queryByText("Discard and record")).toBeNull();
      await act(async () =>
        fireEvent.click(screen.getByText("Discard changes")),
      );
      expect(onDiscard).toHaveBeenCalledOnce();
      expect(start).not.toHaveBeenCalled();
      expect(toast).not.toHaveBeenCalled();
      expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
    },
  );

  it("focuses Save and record when the prompt opens, never Discard", () => {
    setPending();
    renderDialog();
    act(() => requestRecordingStart(vi.fn(), "launcher"));
    expect(document.activeElement?.textContent).toBe("Save and record");
  });

  it("while save is paused, focuses Cancel and disables Save and Discard", () => {
    setPending();
    useWorkflowHasChangesStore.setState({
      saveBlockedReason: "Copilot is applying a change",
    });
    renderDialog();
    act(() => requestRecordingStart(vi.fn(), "launcher"));
    expect(document.activeElement?.textContent).toBe("Cancel");
    expect(
      screen.getByText("Save and record").closest("button")!.disabled,
    ).toBe(true);
    expect(
      screen.getByText("Discard and record").closest("button")!.disabled,
    ).toBe(true);
  });

  it.each([
    [
      "a save",
      { commitInProgress: true, lockKind: "save" as const },
      /A save is in progress/,
    ],
    [
      "a Copilot change",
      { copilotAcceptance: Symbol("copilot") },
      /Wait for the Copilot change to finish/,
    ],
    [
      "an authoring action",
      { authoringInProgress: true },
      /Finish the current authoring action/,
    ],
  ])(
    "while %s holds the editor, focuses Cancel and disables Save and Discard",
    (_, lock, message) => {
      setPending();
      useWorkflowYamlEditorStore.setState(lock);
      renderDialog();
      act(() => requestRecordingStart(vi.fn(), "launcher"));
      expect(document.activeElement?.textContent).toBe("Cancel");
      expect(screen.getByText(message)).toBeTruthy();
      expect(
        screen.getByText("Save and record").closest("button")!.disabled,
      ).toBe(true);
      expect(
        screen.getByText("Discard and record").closest("button")!.disabled,
      ).toBe(true);
    },
  );

  it("reports a discard that could not reload and does not start", async () => {
    setPending();
    renderDialog(undefined, () => {});
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    await act(async () =>
      fireEvent.click(screen.getByText("Discard and record")),
    );
    expect(start).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Couldn't discard changes" }),
    );
  });

  it("does not start after the save when the entry point can no longer record", async () => {
    setPending();
    renderDialog();
    let canRecord = true;
    const start = vi.fn();
    act(() =>
      requestRecordingStart(start, "launcher", {
        isStillValid: () => canRecord,
      }),
    );
    canRecord = false;
    await act(async () =>
      fireEvent.click(screen.getByRole("button", { name: "Save and record" })),
    );
    expect(start).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ title: "Recording didn't start" }),
    );
  });

  it("drops the prompt when its editor is replaced by another workflow's", () => {
    setPending();
    renderDialog();
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    act(() => {
      unregisterEditorOwner(useWorkflowYamlEditorStore.getState().editorOwner!);
      registerEditorOwner(createYamlCommitOwner("wpid_other"));
    });
    expect(
      screen.queryByText("Save or discard changes before recording"),
    ).toBeNull();
    expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
    expect(start).not.toHaveBeenCalled();
  });

  it("drops the prompt when the change resolves elsewhere, so it cannot resurface later", () => {
    setPending();
    renderDialog();
    const start = vi.fn();
    act(() => requestRecordingStart(start, "launcher"));
    act(clearChanges);
    expect(usePendingRecordingStartGate.getState().blockedStart).toBe(null);
    act(() => setPending());
    expect(
      screen.queryByText("Save or discard changes before recording"),
    ).toBeNull();
    expect(start).not.toHaveBeenCalled();
  });

  it("clears a marker owned by a workflow this tab no longer edits and starts", () => {
    setPending("wpid_previous");
    const start = vi.fn();
    requestRecordingStart(start, "launcher");
    expect(start).toHaveBeenCalledOnce();
    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(null);
    expect(blockedEvents()).toHaveLength(0);
  });
});

describe("requestRecordingStart with a stale marker", () => {
  beforeEach(() => {
    deleteRecording.mockReset().mockResolvedValue({});
    useWorkflowYamlEditorStore.setState(
      useWorkflowYamlEditorStore.getInitialState(),
    );
    registerEditorOwner(createYamlCommitOwner("wpid_test"));
    useWorkflowHasChangesStore.setState(
      useWorkflowHasChangesStore.getInitialState(),
    );
    // The editor's save hook owns the recording-deletion callback.
    renderHook(() => useWorkflowSave(), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={new QueryClient()}>
          {children}
        </QueryClientProvider>
      ),
    });
    setPending("wpid_previous");
  });

  afterEach(cleanup);

  it("deletes the abandoned recording it drops", async () => {
    const start = vi.fn();
    requestRecordingStart(start, "launcher");
    expect(start).toHaveBeenCalledOnce();
    await waitFor(() =>
      expect(deleteRecording).toHaveBeenCalledWith(
        "/browser_recordings/br-pending",
      ),
    );
  });

  it.each([
    [true, "keeps"],
    [false, "deletes"],
  ])(
    "when that workflow's save is persisting=%s, %s the recording",
    async (persisting) => {
      useWorkflowYamlEditorStore.setState({
        pendingSaves: {
          wpid_previous: {
            owner: createYamlCommitOwner("wpid_previous"),
            kind: "save",
            persisting,
          },
        },
      });
      const start = vi.fn();
      requestRecordingStart(start, "launcher");
      expect(start).toHaveBeenCalledOnce();
      expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
        null,
      );
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (persisting) expect(deleteRecording).not.toHaveBeenCalled();
      else
        expect(deleteRecording).toHaveBeenCalledWith(
          "/browser_recordings/br-pending",
        );
    },
  );
});
