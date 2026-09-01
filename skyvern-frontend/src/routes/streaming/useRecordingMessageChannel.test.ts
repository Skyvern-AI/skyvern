import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  useRecordingStore,
  type RecordingDraftStep,
} from "@/store/useRecordingStore";

import { useRecordingMessageChannel } from "./useRecordingMessageChannel";

const mocks = vi.hoisted(() => ({
  getWebSocketParams: vi.fn(async () => "client_id=client-test"),
}));

vi.mock("./webSocketParams", () => ({
  useWebSocketParams: () => mocks.getWebSocketParams,
}));

vi.mock("@/util/env", () => ({
  newWssBaseUrl: "wss://api.test",
}));

class MockWebSocket {
  static readonly OPEN = 1;
  static instances: MockWebSocket[] = [];

  readonly readyState = MockWebSocket.OPEN;
  readonly send = vi.fn();
  readonly close = vi.fn();
  onopen: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;

  constructor(readonly url: string) {
    MockWebSocket.instances.push(this);
  }

  emitOpen() {
    this.onopen?.(new Event("open"));
  }

  emitMessage(message: unknown) {
    this.onmessage?.(
      new MessageEvent("message", { data: JSON.stringify(message) }),
    );
  }

  emitClose(code = 1006) {
    this.onclose?.(new CloseEvent("close", { code }));
  }
}

const initialRecordingState = useRecordingStore.getState();

async function openSocket() {
  await act(async () => {
    await Promise.resolve();
  });
  const socket = MockWebSocket.instances[MockWebSocket.instances.length - 1];
  if (!socket) {
    throw new Error("No WebSocket was constructed");
  }
  act(() => socket.emitOpen());
  return socket;
}

describe("useRecordingMessageChannel", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    MockWebSocket.instances = [];
    vi.stubGlobal("WebSocket", MockWebSocket);
    useRecordingStore.setState(initialRecordingState, true);
    useRecordingStore.getState().reset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useRecordingStore.setState(initialRecordingState, true);
  });

  it("sends the complete begin-exfiltration payload on the rising edge", async () => {
    useRecordingStore.setState({
      isRecording: true,
      recordingAttemptId: "attempt-1",
    });
    const { rerender } = renderHook(
      ({ exfiltrate }) =>
        useRecordingMessageChannel({
          browserSessionId: "pbs-1",
          enabled: true,
          exfiltrate,
          workflowPermanentId: "workflow-1",
          clipboard: "none",
        }),
      { initialProps: { exfiltrate: false } },
    );
    const socket = await openSocket();
    socket.send.mockClear();

    rerender({ exfiltrate: true });

    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toContainEqual({
      kind: "begin-exfiltration",
      workflow_permanent_id: "workflow-1",
      live_interpretation_enabled: true,
      supports_interpretation_deltas: true,
      recording_attempt_id: "attempt-1",
    });
  });

  it("sends end-exfiltration when discard and unmount happen together", async () => {
    useRecordingStore.setState({ isRecording: true });
    const { unmount } = renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        exfiltrate: true,
        workflowPermanentId: null,
        clipboard: "none",
      }),
    );
    const socket = await openSocket();

    act(() => useRecordingStore.setState({ isRecording: false }));
    unmount();

    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toEqual([
      expect.objectContaining({ kind: "begin-exfiltration" }),
      { kind: "end-exfiltration" },
    ]);
  });

  it("keeps a mid-recording reconnect resumable without sending end-exfiltration", async () => {
    useRecordingStore.setState({ isRecording: true });
    const { rerender } = renderHook(
      ({ reconnectTrigger }) =>
        useRecordingMessageChannel({
          browserSessionId: "pbs-1",
          enabled: true,
          exfiltrate: true,
          workflowPermanentId: null,
          clipboard: "none",
          reconnectTrigger,
        }),
      { initialProps: { reconnectTrigger: 0 } },
    );
    const firstSocket = await openSocket();

    rerender({ reconnectTrigger: 1 });
    const secondSocket = await openSocket();

    expect(
      firstSocket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).not.toContainEqual({ kind: "end-exfiltration" });
    expect(
      secondSocket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toContainEqual(expect.objectContaining({ kind: "begin-exfiltration" }));
  });

  it("retries begin-exfiltration errors five times per recording", async () => {
    vi.useFakeTimers();
    try {
      useRecordingStore.setState({ isRecording: true });
      const { rerender } = renderHook(
        ({ exfiltrate }) =>
          useRecordingMessageChannel({
            browserSessionId: "pbs-1",
            enabled: true,
            exfiltrate,
            workflowPermanentId: null,
            clipboard: "none",
          }),
        { initialProps: { exfiltrate: true } },
      );
      const socket = await openSocket();

      for (let attempt = 0; attempt < 5; attempt += 1) {
        act(() =>
          socket.emitMessage({
            kind: "error",
            failed_kind: "begin-exfiltration",
            message: "Capture is not ready",
          }),
        );
        await act(async () => vi.advanceTimersByTimeAsync(2000));
      }

      const commands = socket.send.mock.calls.map((call) =>
        JSON.parse(String(call[0])),
      );
      expect(commands).toHaveLength(6);
      expect(commands).toEqual(
        commands.map(() =>
          expect.objectContaining({ kind: "begin-exfiltration" }),
        ),
      );

      act(() =>
        socket.emitMessage({
          kind: "error",
          failed_kind: "begin-exfiltration",
          message: "Capture is still not ready",
        }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(socket.send).toHaveBeenCalledTimes(6);

      rerender({ exfiltrate: false });
      useRecordingStore.setState({ isRecording: true });
      rerender({ exfiltrate: true });
      socket.send.mockClear();
      act(() =>
        socket.emitMessage({
          kind: "error",
          failed_kind: "begin-exfiltration",
          message: "Capture is not ready for the next recording",
        }),
      );
      await act(async () => vi.advanceTimersByTimeAsync(2000));
      expect(
        socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
      ).toContainEqual(expect.objectContaining({ kind: "begin-exfiltration" }));
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not construct a socket after credential loading is cancelled", async () => {
    let resolveParams: ((params: string) => void) | undefined;
    mocks.getWebSocketParams.mockReturnValueOnce(
      new Promise<string>((resolve) => {
        resolveParams = resolve;
      }),
    );
    const { unmount } = renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        exfiltrate: false,
        workflowPermanentId: null,
        clipboard: "none",
      }),
    );

    unmount();
    await act(async () => {
      resolveParams?.("client_id=late");
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toEqual([]);
  });

  it("reports disconnected when credential setup fails", async () => {
    const onConnectionChange = vi.fn();
    mocks.getWebSocketParams.mockRejectedValueOnce(
      new Error("credential lookup failed"),
    );

    renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        exfiltrate: true,
        workflowPermanentId: null,
        clipboard: "none",
        onConnectionChange,
      }),
    );
    await act(async () => {
      await Promise.resolve();
    });

    expect(MockWebSocket.instances).toEqual([]);
    expect(onConnectionChange).toHaveBeenCalledWith(
      false,
      expect.objectContaining({ code: 1011 }),
    );
  });

  it("resets pause edges between exfiltration sessions", async () => {
    const { rerender } = renderHook(
      ({ exfiltrate }) =>
        useRecordingMessageChannel({
          browserSessionId: "pbs-1",
          enabled: true,
          exfiltrate,
          workflowPermanentId: null,
          clipboard: "none",
        }),
      { initialProps: { exfiltrate: true } },
    );
    const socket = await openSocket();

    act(() => useRecordingStore.setState({ manualCapturePaused: true }));
    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toContainEqual({ kind: "recording-capture-pause" });

    rerender({ exfiltrate: false });
    socket.send.mockClear();
    rerender({ exfiltrate: true });

    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ kind: "begin-exfiltration" }),
        { kind: "recording-capture-pause" },
      ]),
    );

    socket.send.mockClear();
    act(() => useRecordingStore.setState({ manualCapturePaused: false }));
    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toContainEqual({ kind: "recording-capture-resume" });
  });

  it("rearms capture after an inbound CDP navigation event", async () => {
    useRecordingStore.setState({ isRecording: true });
    renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        exfiltrate: true,
        workflowPermanentId: null,
        clipboard: "none",
      }),
    );
    const socket = await openSocket();
    socket.send.mockClear();
    const event = {
      kind: "exfiltrated-event",
      event_name: "nav:frame_navigated",
      source: "cdp",
      timestamp: 1,
      params: { targetInfo: { url: "https://example.test/next" } },
    };

    act(() => socket.emitMessage(event));

    expect(
      socket.send.mock.calls.map((call) => JSON.parse(String(call[0]))),
    ).toContainEqual({ kind: "recording-rearm-capture" });
    expect(useRecordingStore.getState().pendingEvents).toContainEqual(event);
  });

  it("applies inbound interpretation updates to the recording store", async () => {
    useRecordingStore.setState({ isRecording: true });
    renderHook(() =>
      useRecordingMessageChannel({
        browserSessionId: "pbs-1",
        enabled: true,
        exfiltrate: true,
        workflowPermanentId: "workflow-1",
        clipboard: "none",
      }),
    );
    const socket = await openSocket();
    const step: RecordingDraftStep = {
      step_id: "step-1",
      action_kind: "click",
      block_type: "action",
      label: "Click submit",
      status: "ready",
      editable_fields: [],
      parameters: [],
      parameter_keys: [],
    };

    act(() =>
      socket.emitMessage({
        kind: "recording-interpretation-update",
        interpretation_session_id: "interpretation-1",
        session_revision: 1,
        steps: [step],
        pending: false,
        finalized: false,
      }),
    );

    expect(useRecordingStore.getState()).toMatchObject({
      interpretationSessionId: "interpretation-1",
      sessionRevision: 1,
      draftSteps: [step],
      interpretationPending: false,
      interpretationFinalized: false,
    });
  });
});
