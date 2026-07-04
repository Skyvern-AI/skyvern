import { describe, expect, it } from "vitest";

import { resolveBrowserPaneView } from "./browserPaneView";

const base = {
  intent: "auto" as const,
  recording: false,
  scrubbing: false,
  inspectingRun: false,
  blockRunInDebugSession: false,
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
    expect(resolveBrowserPaneView({ ...base, hasRecording: true })).toBe(
      "live",
    );
  });

  it("edit entry stays live while the debug session boots, never the latest run's recording", () => {
    // No ?wr= in the URL; the inspected latest run carries a recording and the
    // debug session hasn't booted yet — the pane must be live (connecting).
    expect(resolveBrowserPaneView({ ...base, hasRecording: true })).toBe(
      "live",
    );
    expect(
      resolveBrowserPaneView({ ...base, hasRecording: true, failed: true }),
    ).toBe("live");
  });

  it("falls back to live (warming up) with nothing to show", () => {
    expect(resolveBrowserPaneView(base)).toBe("live");
  });
});
