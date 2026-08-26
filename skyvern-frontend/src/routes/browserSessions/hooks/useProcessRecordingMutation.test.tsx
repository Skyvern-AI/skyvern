// @vitest-environment jsdom

import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { RECORD_BROWSER_V2_FLAG } from "@/util/featureFlags";
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
  enabledFlags: new Set<string>(),
}));

vi.mock("@/api/AxiosClient", () => ({
  getClient: vi.fn(async () => ({ post: mocks.post })),
}));

vi.mock("@/components/ui/use-toast", () => ({ toast: vi.fn() }));

vi.mock("@/hooks/useCredentialGetter", () => ({
  useCredentialGetter: () => vi.fn(async () => "test-token"),
}));

vi.mock("posthog-js/react", () => ({
  useFeatureFlagEnabled: (flag: string) => mocks.enabledFlags.has(flag),
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

/** A message socket that answers the commit it is sent with a scripted reply. */
function fakeMessageSocket() {
  const listeners = new Set<(event: MessageEvent) => void>();
  let reply: unknown = null;
  const socket = {
    readyState: WebSocket.OPEN,
    sent: [] as Array<unknown>,
    reply: (message: unknown) => {
      reply = message;
    },
    send: (payload: string) => {
      socket.sent.push(JSON.parse(payload));
      const event = new MessageEvent("message", {
        data: JSON.stringify(reply),
      });
      for (const listener of [...listeners]) {
        listener(event);
      }
    },
    addEventListener: (
      type: string,
      listener: (event: MessageEvent) => void,
    ) => (type === "message" ? listeners.add(listener) : undefined),
    removeEventListener: (
      _type: string,
      listener: (event: MessageEvent) => void,
    ) => listeners.delete(listener),
  };
  return socket as typeof socket & WebSocket;
}

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
    mocks.enabledFlags.clear();
    useRecordingStore.getState().reset();
    useRecordingStore.getState().setMessageSocket(null);
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

  it("commits over the message socket instead of HTTP when record browser v2 is on", async () => {
    mocks.enabledFlags.add(RECORD_BROWSER_V2_FLAG);
    const blocks = [{ block_type: "action", label: "click_search" }];
    const socket = fakeMessageSocket();
    useRecordingStore.getState().setMessageSocket(socket);
    socket.reply({
      kind: "recording-committed",
      blocks,
      parameters: [],
      mode: "blocks",
      diagnostics: { rows: 2, facts: 1, dropped: 0, unlocatable: 0 },
    });
    const onSuccess = vi.fn();
    const { result } = renderHook(
      () =>
        useProcessRecordingMutation({ browserSessionId: "pbs-1", onSuccess }),
      { wrapper },
    );

    act(() => result.current.mutate({ draftSteps: [draftStep] }));

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(socket.sent).toEqual([
      { kind: "recording-commit", mode: "auto", draft_steps: [draftStep] },
    ]);
    expect(mocks.post).not.toHaveBeenCalled();
    expect(onSuccess).toHaveBeenCalledWith({ blocks, parameters: [] });
  });
});
