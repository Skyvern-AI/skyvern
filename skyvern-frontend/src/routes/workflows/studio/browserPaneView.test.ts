import { describe, expect, it } from "vitest";

import { resolveBrowserPaneView, resolveLiveSurface } from "./browserPaneView";

// Most cases inspect an open run; edit-context cases override inspectingRun.
const base = {
  intent: "auto" as const,
  recording: false,
  scrubbing: false,
  inspectingRun: true,
  blockRunInDebugSession: false,
  systemFocused: false,
  runInDebugSession: false,
  running: false,
  hasRecording: false,
  failed: false,
};

describe("resolveBrowserPaneView", () => {
  it("pins Live regardless of run state", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "live",
        inspectingRun: true,
        hasRecording: true,
      }),
    ).toBe("live");
  });

  it("pins Recording even before its data arrives (empty state)", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "recording",
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(resolveBrowserPaneView({ ...base, intent: "recording" })).toBe(
      "recording",
    );
  });

  it("pins Screenshots even before its data arrives (empty state)", () => {
    expect(resolveBrowserPaneView({ ...base, intent: "screenshots" })).toBe(
      "screenshots",
    );
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "screenshots",
        hasRecording: true,
      }),
    ).toBe("screenshots");
  });

  it("overrides a stored replay intent when a recording starts", () => {
    for (const intent of ["recording", "screenshots"] as const) {
      expect(
        resolveBrowserPaneView({
          ...base,
          intent,
          recording: true,
          hasRecording: true,
        }),
      ).toBe("live");
    }
  });

  it("pins live while a browser recording is in progress", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        recording: true,
        scrubbing: true,
        inspectingRun: true,
        hasRecording: true,
      }),
    ).toBe("live");
  });

  it("shows the selected step while scrubbing", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        scrubbing: true,
        running: true,
      }),
    ).toBe("screenshots");
  });

  it("keeps a system-focused run live while it is running", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        inspectingRun: true,
        running: true,
      }),
    ).toBe("live");
  });

  it("keeps a system-focused run that ran in the debug session live after it finishes", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        inspectingRun: true,
        runInDebugSession: true,
        running: false,
        hasRecording: true,
      }),
    ).toBe("live");
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        inspectingRun: true,
        runInDebugSession: true,
        running: false,
        failed: true,
      }),
    ).toBe("live");
  });

  it("replays a finished system-focused run that ran in its own browser", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        inspectingRun: true,
        runInDebugSession: false,
        running: false,
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        inspectingRun: true,
        runInDebugSession: false,
        running: false,
        failed: true,
        hasRecording: true,
      }),
    ).toBe("screenshots");
  });

  it("keeps explicit replay authoritative during system focus", () => {
    for (const intent of ["recording", "screenshots"] as const) {
      expect(
        resolveBrowserPaneView({ ...base, systemFocused: true, intent }),
      ).toBe(intent);
    }
    expect(
      resolveBrowserPaneView({
        ...base,
        systemFocused: true,
        scrubbing: true,
      }),
    ).toBe("screenshots");
  });

  it("stays live for a block run in the debug session, even finalized", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        blockRunInDebugSession: true,
        inspectingRun: true,
        hasRecording: true,
      }),
    ).toBe("live");
  });

  it("goes live while the inspected run is running", () => {
    expect(
      resolveBrowserPaneView({ ...base, inspectingRun: true, running: true }),
    ).toBe("live");
  });

  it("replays a finished inspected run (recording first)", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        inspectingRun: true,
        hasRecording: true,
      }),
    ).toBe("recording");
  });

  it("shows screenshots for a failed inspected run", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        inspectingRun: true,
        failed: true,
        hasRecording: true,
      }),
    ).toBe("screenshots");
  });

  it("defaults to the live debug browser when no run is inspected", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        inspectingRun: false,
        hasRecording: true,
      }),
    ).toBe("live");
  });

  it("edit entry stays live while the debug session boots, never the latest run's recording", () => {
    // No ?wr= in the URL; the inspected latest run carries a recording and the
    // debug session hasn't booted yet — the pane must be live (connecting).
    const edit = { ...base, inspectingRun: false, hasRecording: true };
    expect(resolveBrowserPaneView(edit)).toBe("live");
    expect(resolveBrowserPaneView({ ...edit, failed: true })).toBe("live");
  });

  it("never replays without an open run, whatever the stored intent or step pin", () => {
    // The replay pills are hidden with no run open, so a pill intent left over
    // from a closed run or a latest-run ?active= pin must not strand the pane
    // on an old run's replay (or on a zero-run workflow's endless empty state).
    const edit = { ...base, inspectingRun: false };
    for (const intent of ["recording", "screenshots"] as const) {
      expect(resolveBrowserPaneView({ ...edit, intent })).toBe("live");
    }
    expect(resolveBrowserPaneView({ ...edit, scrubbing: true })).toBe("live");
    expect(
      resolveBrowserPaneView({ ...edit, systemFocused: true, scrubbing: true }),
    ).toBe("live");
  });

  it("falls back to live (warming up) with nothing to show", () => {
    expect(resolveBrowserPaneView({ ...base, inspectingRun: false })).toBe(
      "live",
    );
  });
});

const liveBase = {
  recording: false,
  running: false,
  runInDebugSession: false,
  hasRunId: false,
};

describe("resolveLiveSurface", () => {
  it("follows a run that is running outside the debug session", () => {
    expect(
      resolveLiveSurface({ ...liveBase, running: true, hasRunId: true }),
    ).toBe("run");
  });

  it("returns to the debug session once that run is terminal", () => {
    expect(
      resolveLiveSurface({ ...liveBase, running: false, hasRunId: true }),
    ).toBe("debug");
  });

  it("stays on the debug session for a run executing in it", () => {
    expect(
      resolveLiveSurface({
        ...liveBase,
        running: true,
        runInDebugSession: true,
        hasRunId: true,
      }),
    ).toBe("debug");
  });

  it("stays on the debug session while recording", () => {
    expect(
      resolveLiveSurface({
        ...liveBase,
        recording: true,
        running: true,
        hasRunId: true,
      }),
    ).toBe("debug");
  });
});
