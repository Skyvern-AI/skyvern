import { describe, expect, it } from "vitest";

import { resolveBrowserPaneView } from "./browserPaneView";

const base = {
  intent: "auto" as const,
  recording: false,
  scrubbing: false,
  inspectingRun: false,
  blockRunInDebugSession: false,
  running: false,
  debugSessionUp: false,
  hasRecording: false,
  hasScreenshots: false,
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
        hasScreenshots: true,
      }),
    ).toBe("live");
  });

  it("pins Recording only when a recording exists", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "recording",
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "recording",
        debugSessionUp: true,
      }),
    ).toBe("live");
  });

  it("pins Screenshots only when screenshots exist", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "screenshots",
        hasScreenshots: true,
      }),
    ).toBe("screenshots");
    expect(
      resolveBrowserPaneView({
        ...base,
        intent: "screenshots",
        debugSessionUp: true,
      }),
    ).toBe("live");
  });

  it("overrides a stored replay intent when a recording starts", () => {
    for (const intent of ["recording", "screenshots"] as const) {
      expect(
        resolveBrowserPaneView({
          ...base,
          intent,
          recording: true,
          hasRecording: true,
          hasScreenshots: true,
          debugSessionUp: true,
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
        hasScreenshots: true,
      }),
    ).toBe("live");
  });

  it("shows the selected step while scrubbing", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        scrubbing: true,
        running: true,
        debugSessionUp: true,
      }),
    ).toBe("screenshots");
  });

  it("stays live for a block run in the debug session, even finalized", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        blockRunInDebugSession: true,
        inspectingRun: true,
        debugSessionUp: true,
        hasRecording: true,
        hasScreenshots: true,
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
        debugSessionUp: true,
        hasRecording: true,
        hasScreenshots: true,
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
        hasScreenshots: true,
      }),
    ).toBe("screenshots");
  });

  it("defaults to the live debug browser when no run is inspected", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        debugSessionUp: true,
        hasRecording: true,
        hasScreenshots: true,
      }),
    ).toBe("live");
  });

  it("defaults to the last run's replay when idle with history and no session", () => {
    expect(
      resolveBrowserPaneView({
        ...base,
        hasRecording: true,
        hasScreenshots: true,
      }),
    ).toBe("recording");
    expect(resolveBrowserPaneView({ ...base, hasScreenshots: true })).toBe(
      "screenshots",
    );
  });

  it("falls back to live (warming up) with nothing to show", () => {
    expect(resolveBrowserPaneView(base)).toBe("live");
  });
});
