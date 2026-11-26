import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamStateChangeHandler } from "@/routes/streaming/streamState";
import { WorkflowRunStream } from "./WorkflowRunStream";

vi.mock("@/util/env", () => ({
  getWebSocketProtocols: vi.fn(async () => ["skyvern.v1", "apikey.test"]),
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

const interactiveStreamState = vi.hoisted(() => ({
  setUserIsControlling: undefined as ((v: boolean) => void) | undefined,
}));

vi.mock("@/routes/streaming/InteractiveStreamView", () => ({
  InteractiveStreamView: ({
    streamImgSrc,
    setUserIsControlling,
  }: {
    streamImgSrc: string;
    setUserIsControlling?: (v: boolean) => void;
  }) => {
    interactiveStreamState.setUserIsControlling = setUserIsControlling;
    return <div data-frame={streamImgSrc} data-testid="stream-frame" />;
  },
}));

class FakeStreamSocket {
  static readonly OPEN = 1;
  static instances: FakeStreamSocket[] = [];

  readonly readyState = FakeStreamSocket.OPEN;
  url: string;
  send = vi.fn();
  close = vi.fn();

  private listeners: Record<string, Array<(event: unknown) => void>> = {};

  constructor(url: string) {
    this.url = url;
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

function renderStream(
  props: { onStreamStateChange?: StreamStateChangeHandler } = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <WorkflowRunStream workflowRunId="wr_test" alwaysShowStream {...props} />
    </QueryClientProvider>,
  );
}

async function streamOneFrame(props?: Parameters<typeof renderStream>[0]) {
  renderStream(props);
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
    cleanup();
    interactiveStreamState.setUserIsControlling = undefined;
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
    const onStreamStateChange = vi.fn();
    const socket = await streamOneFrame({ onStreamStateChange });
    expect(onStreamStateChange).toHaveBeenLastCalledWith("live", null);

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
    expect(onStreamStateChange).toHaveBeenLastCalledWith("stopped", null);
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

  it("renders the console and evaluates expressions over the workflow_run input channel when enabled", async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <WorkflowRunStream
          workflowRunId="wr_test"
          alwaysShowStream
          interactive
          showControlButtons
          showConsole
        />
      </QueryClientProvider>,
    );
    await act(async () => Promise.resolve());

    const streamSocket = FakeStreamSocket.instances.find((s) =>
      s.url.includes("/stream/workflow_runs/wr_test"),
    )!;
    const inputSocket = FakeStreamSocket.instances.find((s) =>
      s.url.includes("/stream/cdp_input/workflow_run/wr_test"),
    )!;
    expect(inputSocket).toBeTruthy();

    act(() => {
      streamSocket.emitStreamMessage({
        status: "running",
        screenshot: "live-frame",
      });
      inputSocket.emitStreamMessage({ kind: "ready" });
      inputSocket.emitStreamMessage({ kind: "console", available: true });
    });

    const toggle = screen.getByRole("button", { name: /console/i });
    fireEvent.click(toggle);
    act(() => interactiveStreamState.setUserIsControlling?.(true));

    const textarea = screen.getByLabelText("JavaScript to run in the page");
    fireEvent.change(textarea, { target: { value: "document.title" } });
    fireEvent.click(screen.getByRole("button", { name: "Run" }));

    const evalMessage = inputSocket.send.mock.calls
      .map((call) => JSON.parse(String(call[0])))
      .find((msg) => msg.kind === "evaluate");
    expect(evalMessage).toMatchObject({
      kind: "evaluate",
      expression: "document.title",
    });

    await act(async () => {
      inputSocket.emitStreamMessage({
        kind: "evaluateResult",
        id: evalMessage.id,
        status: "ok",
        text: '"Example Title"',
        url: "https://example.test/",
      });
    });

    expect(screen.getByText('"Example Title"')).toBeTruthy();
  });
});
