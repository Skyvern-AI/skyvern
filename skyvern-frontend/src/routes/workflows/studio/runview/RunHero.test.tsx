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

describe("RunHero header dedupe", () => {
  test("recording view does not echo the active toggle label in the header", () => {
    render(
      <RunHero
        {...baseProps}
        showDebugStream={false}
        running={false}
        recordingUrls={["https://example.com/rec.mp4"]}
      />,
    );
    // Only the toggle should say "Recording" — not the header label too.
    expect(screen.getAllByText("Recording")).toHaveLength(1);
  });

  test("code view does not echo the active toggle as a header label", () => {
    useRunViewStore.getState().setCenterView("code");
    render(<RunHero {...baseProps} showDebugStream={false} />);
    // The Code toggle communicates the view; the "Generated code" header is redundant.
    expect(screen.queryByText("Generated code")).toBeNull();
  });

  test("scrubbed action description appears once, not in the header too", () => {
    useRunViewStore.getState().pinFrame("f1");
    render(
      <RunHero
        {...baseProps}
        showDebugStream={false}
        running={false}
        heroSelection={{ kind: "thought", thoughtId: "t1" }}
        heroLabel="Click the login button"
      />,
    );
    // The in-card "Inspecting step" bar owns the action description.
    expect(screen.getAllByText("Click the login button")).toHaveLength(1);
  });

  test("view toggles live in the labeled segmented control, on the leading edge", () => {
    render(<RunHero {...baseProps} showDebugStream={false} />);
    const group = screen.getByRole("group", { name: "Center view" });
    expect(group.contains(screen.getByText("Live"))).toBe(true);
    expect(group.contains(screen.getByText("Code"))).toBe(true);
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
