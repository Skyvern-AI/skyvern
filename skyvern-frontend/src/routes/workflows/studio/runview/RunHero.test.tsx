// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  heroSelection: null,
  heroLabel: "",
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

describe("RunHero Code surface (single source of truth)", () => {
  test("the Code toggle opens the generated-code view", () => {
    render(<RunHero {...baseProps} showDebugStream={false} />);
    expect(screen.queryByTestId("workflow-run-code")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Code" }));
    expect(screen.queryByTestId("workflow-run-code")).not.toBeNull();
  });

  test("the Code toggle owns code-generation state, showing a spinner while generating", () => {
    const { rerender } = render(
      <RunHero {...baseProps} showDebugStream={false} codeGenerating={false} />,
    );
    expect(screen.queryByTestId("code-generating-spinner")).toBeNull();

    rerender(<RunHero {...baseProps} showDebugStream={false} codeGenerating />);
    expect(screen.queryByTestId("code-generating-spinner")).not.toBeNull();
  });
});

describe("RunHero failure banner", () => {
  const failedProps = {
    ...baseProps,
    showDebugStream: true,
    running: false,
    failed: true,
    failureReason: "for_loop block failed.",
  };

  test("shows the failure reason with a dismiss button when the run failed", () => {
    render(<RunHero {...failedProps} />);
    expect(screen.queryByText("for_loop block failed.")).not.toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeNull();
  });

  test("dismiss hides the failure banner", () => {
    render(<RunHero {...failedProps} />);
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("for_loop block failed.")).toBeNull();
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });

  test("no failure banner when the run has not failed", () => {
    render(<RunHero {...failedProps} failed={false} />);
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
  });
});
