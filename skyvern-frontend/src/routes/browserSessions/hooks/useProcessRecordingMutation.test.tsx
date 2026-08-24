// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import {
  useRecordingStore,
  type OptimisticStep,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";

import { useProcessRecordingMutation } from "./useProcessRecordingMutation";

const mocks = vi.hoisted(() => ({
  captureRecordBrowser: vi.fn(),
  markRecordBrowserProcessed: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ post: mocks.post })),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(async () => "test-token"),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: () => false,
}));

vi.mock("@/util/recordBrowserTelemetry", () => ({
  captureRecordBrowser: mocks.captureRecordBrowser,
  markRecordBrowserProcessed: mocks.markRecordBrowserProcessed,
}));

function wrapper({ children }: { children: ReactNode }) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false } },
  });
  return (
    <WorkflowPermanentIdContext.Provider value="wpid-1">
      <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
    </WorkflowPermanentIdContext.Provider>
  );
}

const draftStep: RecordingDraftStep = {
  step_id: "step-1",
  action_kind: "click",
  block_type: "action",
  label: "Click",
  status: "ready",
  editable_fields: [],
  parameters: [],
  parameter_keys: [],
};

const optimisticStep: OptimisticStep = {
  local_id: "optimistic-1",
  action_kind: "click",
  title: "Click",
  timestamp: 1,
};

describe("useProcessRecordingMutation telemetry", () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(1_000);
    vi.clearAllMocks();
    useRecordingStore.getState().reset();
    useRecordingStore.getState().setRecordingTransport("cdp");
    useRecordingStore.getState().setIsRecording(true);
    useRecordingStore.setState({ optimisticSteps: [optimisticStep] });
    mocks.captureRecordBrowser.mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
    useRecordingStore.getState().reset();
  });

  it("reports successful processing with the recording transport and counters", async () => {
    mocks.post.mockResolvedValue({ data: { blocks: [], parameters: [] } });
    vi.setSystemTime(3_500);
    useRecordingStore.getState().setIsRecording(false);
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({
          browserSessionId: "pbs-1",
        }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(mocks.captureRecordBrowser).toHaveBeenCalledWith(
      "record_browser.finished",
      {
        transport: "cdp",
        duration_ms: 2_500,
        event_count: 0,
        optimistic_step_count: 1,
      },
    );
  });
});
