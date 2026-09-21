// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { parse as parseYaml } from "yaml";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  useWorkflowHasChangesStore,
  useWorkflowSave,
  type WorkflowSaveData,
} from "./WorkflowHasChangesStore";

const mocks = vi.hoisted(() => ({
  delete: vi.fn(),
  getClient: vi.fn(),
  put: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: mocks.getClient,
}));
vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));
vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(async () => "test-token"),
}));
vi.mock("posthog-js/react", () => ({
  usePostHog: () => ({ capture: vi.fn() }),
}));

function wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={new QueryClient()}>
      {children}
    </QueryClientProvider>
  );
}

const saveData = {
  title: "Recorded workflow",
  blocks: [],
  parameters: [],
  workflowDefinitionVersion: 1,
  settings: {
    proxyLocation: "RESIDENTIAL",
    runWith: "agent",
  },
  workflow: {
    workflow_permanent_id: "wpid-1",
    workflow_definition: { version: 1, blocks: [], parameters: [] },
    status: "published",
  },
} as unknown as WorkflowSaveData;

describe("workflow recording attachment", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getClient.mockResolvedValue({
      delete: mocks.delete,
      put: mocks.put,
    });
    mocks.put.mockResolvedValue({ data: {} });
    useWorkflowHasChangesStore.setState({
      getSaveData: () => saveData,
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("sends the pending recording on save and clears it only after success", async () => {
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => result.current.mutateAsync(undefined));

    const call = mocks.put.mock.calls[0];
    expect(call).toBeDefined();
    const yamlBody = call?.[1] as string;
    expect(parseYaml(yamlBody)).toMatchObject({ recording_id: "br-1" });
    await waitFor(() =>
      expect(
        useWorkflowHasChangesStore.getState().pendingRecordingId,
      ).toBeNull(),
    );
    expect(mocks.delete).not.toHaveBeenCalled();
  });

  it("deletes an unattached recording when changes are discarded", async () => {
    renderHook(() => useWorkflowSave(), { wrapper });

    useWorkflowHasChangesStore.getState().setHasChanges(false);

    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: false,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
    });
    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
    expect(mocks.getClient).toHaveBeenCalledWith(
      expect.any(Function),
      "sans-api-v1",
    );
  });

  it("keeps discard deletion registered while another save hook remains mounted", async () => {
    renderHook(() => useWorkflowSave(), { wrapper });
    const latestHook = renderHook(() => useWorkflowSave(), { wrapper });
    latestHook.unmount();

    useWorkflowHasChangesStore.getState().setHasChanges(false);

    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
  });

  it("discards a recording for another workflow after this workflow saves", async () => {
    useWorkflowHasChangesStore.setState({
      pendingRecordingWorkflowPermanentId: "wpid-other",
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => result.current.mutateAsync(undefined));

    await waitFor(() =>
      expect(mocks.delete).toHaveBeenCalledWith("/browser_recordings/br-1"),
    );
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: false,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
    });
  });

  it("does not replace the first recording waiting to be saved", () => {
    useWorkflowHasChangesStore.getState().setPendingRecording("br-2", "wpid-1");

    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });

  it("keeps the pending recording when the workflow save fails", async () => {
    mocks.put.mockRejectedValue(new Error("save failed"));
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow("save failed");

    expect(useWorkflowHasChangesStore.getState().pendingRecordingId).toBe(
      "br-1",
    );
  });

  it("keeps the pending recording when header validation fails", async () => {
    useWorkflowHasChangesStore.setState({
      getSaveData: () => ({
        ...saveData,
        settings: { ...saveData.settings, extraHttpHeaders: "{" },
      }),
      hasChanges: true,
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await expect(
      act(async () => result.current.mutateAsync(undefined)),
    ).rejects.toThrow();

    expect(mocks.put).not.toHaveBeenCalled();
    expect(mocks.delete).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState()).toMatchObject({
      hasChanges: true,
      pendingRecordingId: "br-1",
      pendingRecordingWorkflowPermanentId: "wpid-1",
    });
  });
});

describe("blocked saves", () => {
  it("refuses to write the workflow and keeps it dirty while a save is blocked", async () => {
    mocks.getClient.mockResolvedValue({ delete: mocks.delete, put: mocks.put });
    useWorkflowHasChangesStore.setState({
      getSaveData: () => saveData,
      hasChanges: true,
      pendingRecordingId: null,
      pendingRecordingWorkflowPermanentId: null,
      saveBlockedReason: "An Accept's outcome is still unconfirmed.",
    });
    const { result } = renderHook(() => useWorkflowSave(), { wrapper });

    await act(async () => {
      await expect(result.current.mutateAsync(undefined)).rejects.toBeTruthy();
    });

    expect(mocks.put).not.toHaveBeenCalled();
    expect(useWorkflowHasChangesStore.getState().hasChanges).toBe(true);
    useWorkflowHasChangesStore.setState({ saveBlockedReason: null });
  });
});
