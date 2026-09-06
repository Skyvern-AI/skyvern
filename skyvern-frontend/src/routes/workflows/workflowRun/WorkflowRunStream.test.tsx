import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkflowRunStream } from "./WorkflowRunStream";

vi.mock("@/util/env", () => ({
  getCredentialParam: vi.fn(async () => "apikey=test"),
}));

vi.mock("@/hooks/useCredentialGetter", () => {
  // Stable identity: the stream effect keys on it, and a fresh function per
  // render would tear the socket down and clear the frame on every state change.
  const credentialGetter = async () => null;
  return { useCredentialGetter: () => credentialGetter };
});

vi.mock("@/hooks/useFirstParam", () => ({
  useFirstParam: () => "wr_test",
}));

// Mirrors the hook's own resolution: an options object naming an absent run
// means no run, while omitted options defer to the route's :workflowRunId.
const { runQueryMock } = vi.hoisted(() => ({
  runQueryMock: vi.fn((options?: { workflowRunId?: string }) => ({
    data:
      options && !options.workflowRunId
        ? undefined
        : {
            status: "running",
            workflow_run_id: "wr_test",
            workflow: { workflow_permanent_id: "wpid_test" },
          },
  })),
}));

vi.mock("../hooks/useWorkflowRunWithWorkflowQuery", () => ({
  useWorkflowRunWithWorkflowQuery: runQueryMock,
}));

vi.mock("@/components/ui/use-toast", () => ({
  toast: vi.fn(),
}));

vi.mock("@/routes/streaming/InteractiveStreamView", () => ({
  InteractiveStreamView: ({ streamImgSrc }: { streamImgSrc: string }) => (
    <div data-frame={streamImgSrc} data-testid="stream-frame" />
  ),
}));

class FakeStreamSocket {
  static instances: FakeStreamSocket[] = [];

  close = vi.fn();

  private listeners: Record<string, Array<(event: unknown) => void>> = {};

  constructor() {
    FakeStreamSocket.instances.push(this);
  }

  addEventListener(type: string, listener: (event: unknown) => void) {
    this.listeners[type] = [...(this.listeners[type] ?? []), listener];
  }

  removeEventListener() {}

  emit(type: string, event: unknown) {
    for (const listener of this.listeners[type] ?? []) {
      listener(event);
    }
  }

  emitStreamMessage(message: Record<string, unknown>) {
    this.emit("message", { data: JSON.stringify(message) });
  }
}

function renderStream() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowRunStream workflowRunId="wr_test" alwaysShowStream />
    </QueryClientProvider>,
  );
}

async function streamOneFrame() {
  renderStream();
  await act(async () => Promise.resolve());
  const socket = FakeStreamSocket.instances[0]!;
  act(() => {
    socket.emitStreamMessage({ status: "running", screenshot: "live-frame" });
  });
  expect(screen.getByTestId("stream-frame")).toBeTruthy();
  return socket;
}

describe("WorkflowRunStream lifecycle", () => {
  beforeEach(() => {
    FakeStreamSocket.instances.length = 0;
    vi.clearAllMocks();
    vi.useFakeTimers();
    vi.stubGlobal("WebSocket", FakeStreamSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("retires the stale frame and reconnects when the stream ends non-terminally (SKY-14617)", async () => {
    const socket = await streamOneFrame();

    // What the backend sends once its screencast loop returns for a run that is
    // still going: a bare, non-terminal status, then a clean close.
    act(() => {
      socket.emitStreamMessage({ status: "running" });
    });
    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(
      screen.getByText("This live view has stopped updating"),
    ).toBeTruthy();

    act(() => {
      socket.emit("close", { code: 1000, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(FakeStreamSocket.instances).toHaveLength(2);
  });

  it("reports and retries a close that arrives after frames (SKY-14617)", async () => {
    const socket = await streamOneFrame();

    act(() => {
      socket.emit("close", { code: 1006, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));

    expect(FakeStreamSocket.instances).toHaveLength(2);
  });

  it("treats timeout as terminal, matching BrowserSessionStream", async () => {
    const socket = await streamOneFrame();

    act(() => {
      socket.emitStreamMessage({ status: "timeout" });
    });
    expect(screen.queryByTestId("stream-frame")).toBeNull();
    expect(screen.getByText("The browser's gone strangely quiet")).toBeTruthy();

    act(() => {
      socket.emit("close", { code: 1000, reason: "" });
    });
    await act(async () => vi.advanceTimersByTimeAsync(1000));
    expect(FakeStreamSocket.instances).toHaveLength(1);
  });

  it("streams the route's run when rendered without a run id", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <WorkflowRunStream />
      </QueryClientProvider>,
    );
    await act(async () => Promise.resolve());

    act(() => {
      FakeStreamSocket.instances[0]!.emitStreamMessage({
        status: "running",
        screenshot: "live-frame",
      });
    });
    expect(screen.getByTestId("stream-frame")).toBeTruthy();
  });
});
