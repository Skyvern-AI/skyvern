// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { useRunViewStore } from "@/store/RunViewStore";

vi.mock("./RunLiveStream", () => ({
  RunLiveStream: () => <div data-testid="run-live-stream" />,
}));
vi.mock("../../workflowRun/WorkflowRunCode", () => ({
  WorkflowRunCode: () => <div data-testid="workflow-run-code" />,
}));
vi.mock("./HeroRecording", () => ({
  HeroRecording: () => <div data-testid="hero-recording" />,
}));
vi.mock("./HeroScreenshot", () => ({
  HeroScreenshot: () => <div data-testid="hero-screenshot" />,
}));

import { RunHero } from "./RunHero";

const baseProps = {
  workflowRunId: "wr_1",
  shownFrame: null,
  running: true,
  provisioning: false,
  isPaused: false,
  failed: false,
  failureReason: null,
  browserSessionId: "bp_1",
  recordingUrls: [],
  elapsed: "0:01",
};

afterEach(cleanup);
beforeEach(() => useRunViewStore.getState().reset());

describe("RunHero block-run stream", () => {
  test("block run shows the shared debug-stream slot, not its own RunLiveStream", () => {
    render(<RunHero {...baseProps} showDebugStream />);
    expect(screen.queryByTestId("run-stream-slot")).not.toBeNull();
    expect(screen.queryByTestId("run-live-stream")).toBeNull();
  });

  test("full run shows its own RunLiveStream, not the debug-stream slot", () => {
    render(<RunHero {...baseProps} showDebugStream={false} />);
    expect(screen.queryByTestId("run-live-stream")).not.toBeNull();
    expect(screen.queryByTestId("run-stream-slot")).toBeNull();
  });

  test("block run skips the provisioning panel (debug stream is already alive)", () => {
    render(
      <RunHero {...baseProps} showDebugStream running={false} provisioning />,
    );
    expect(screen.queryByTestId("run-stream-slot")).not.toBeNull();
  });
});
