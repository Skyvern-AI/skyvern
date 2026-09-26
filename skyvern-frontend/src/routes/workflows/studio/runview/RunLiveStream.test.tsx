// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useEffect } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { StreamStateChangeHandler } from "@/routes/streaming/streamState";

let transport: string | undefined = "cdp";

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useStreamTransport: () => ({ streamTransport: transport }),
}));

vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: ({
    onClose,
    onStreamStateChange,
  }: {
    onClose: () => void;
    onStreamStateChange?: StreamStateChangeHandler;
  }) => (
    <button
      data-testid="vnc-stream"
      data-reports-state={onStreamStateChange ? "yes" : "no"}
      onClick={onClose}
    />
  ),
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: ({
    onStreamStateChange,
  }: {
    onStreamStateChange?: StreamStateChangeHandler;
  }) => {
    useEffect(() => {
      onStreamStateChange?.("live", "pbs_1");
    }, [onStreamStateChange]);
    return <div data-testid="session-stream" />;
  },
}));

vi.mock("../../workflowRun/WorkflowRunStream", () => ({
  WorkflowRunStream: () => <div data-testid="run-stream" />,
}));

import { RunLiveStream } from "./RunLiveStream";

afterEach(cleanup);

describe("RunLiveStream", () => {
  test("streams the session, not the per-run key, on the cdp transport", () => {
    transport = "cdp";
    render(
      <RunLiveStream
        workflowRunId="wr_1"
        browserSessionId="pbs_1"
        interactive={false}
      />,
    );
    expect(screen.queryByTestId("session-stream")).not.toBeNull();
    expect(screen.queryByTestId("run-stream")).toBeNull();
  });

  test("keeps VNC for a session that serves it", () => {
    transport = "vnc";
    render(
      <RunLiveStream
        workflowRunId="wr_1"
        browserSessionId="pbs_1"
        interactive={false}
      />,
    );
    expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
  });

  test("hands stream state to the CDP fallback once VNC closes", () => {
    transport = "vnc";
    const onStreamStateChange = vi.fn();
    render(
      <RunLiveStream
        workflowRunId="wr_1"
        browserSessionId="pbs_1"
        interactive={false}
        onStreamStateChange={onStreamStateChange}
      />,
    );
    expect(
      screen.getByTestId("vnc-stream").getAttribute("data-reports-state"),
    ).toBe("yes");
    fireEvent.click(screen.getByTestId("vnc-stream"));

    expect(screen.queryByTestId("session-stream")).not.toBeNull();
    expect(onStreamStateChange).toHaveBeenLastCalledWith("live", "pbs_1");
  });

  test("streams the per-run key when the run has no browser session", () => {
    transport = "vnc";
    render(
      <RunLiveStream
        workflowRunId="wr_1"
        browserSessionId={null}
        interactive={false}
      />,
    );
    expect(screen.queryByTestId("run-stream")).not.toBeNull();
  });
});

test("remounts the whole transport chooser when an attempt changes without a wait frame", () => {
  transport = "vnc";
  const run = {
    workflow_run_id: "wr_attempt",
    status: "running" as const,
    attempt: 1,
  };
  const { rerender } = render(
    <RunLiveStream
      workflowRunId={run.workflow_run_id}
      run={run}
      browserSessionId="pbs_same"
      interactive={false}
    />,
  );
  fireEvent.click(screen.getByTestId("vnc-stream"));
  expect(screen.queryByTestId("session-stream")).not.toBeNull();
  rerender(
    <RunLiveStream
      workflowRunId={run.workflow_run_id}
      run={{ ...run, attempt: 2 }}
      browserSessionId="pbs_same"
      interactive={false}
    />,
  );
  expect(screen.queryByTestId("vnc-stream")).not.toBeNull();
  expect(screen.queryByTestId("session-stream")).toBeNull();
});

test("shows retry waits through the theme-aware status panel", () => {
  render(
    <RunLiveStream
      workflowRunId="wr_wait"
      run={{
        workflow_run_id: "wr_wait",
        status: "failed",
        retry_pending: true,
      }}
      browserSessionId={null}
      interactive={false}
    />,
  );
  const panel = screen.getByRole("status");
  expect(panel.textContent).toContain("Retry pending");
  expect(panel.textContent).toContain(
    "The browser reconnects when the next attempt starts.",
  );
  expect(panel.className).toContain("bg-white");
  expect(panel.className).toContain("text-neutral-600");
  expect(screen.queryByTestId("run-stream")).toBeNull();
});
