// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

let transport: string | undefined = "cdp";

vi.mock("@/hooks/useRuntimeConfig", () => ({
  useStreamTransport: () => ({ streamTransport: transport }),
}));

vi.mock("@/components/BrowserStream", () => ({
  BrowserStream: () => <div data-testid="vnc-stream" />,
}));

vi.mock("@/routes/browserSessions/BrowserSessionStream", () => ({
  BrowserSessionStream: () => <div data-testid="session-stream" />,
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
