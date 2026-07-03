import { describe, expect, test } from "vitest";

import { resolveRunHeroCenterView } from "./runHeroCenter";

const defaultArgs = {
  centerView: "default" as const,
  hasScreenshots: false,
  hasInputs: false,
  hasOutputs: false,
  hasRecording: false,
  scrubbing: false,
  showDebugStream: false,
  debugStreamInBrowserPane: false,
  recordingOpen: false,
  running: false,
  failed: false,
};

describe("resolveRunHeroCenterView", () => {
  test("uses the screenshots tab when screenshots are available", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "screenshots",
        hasScreenshots: true,
      }),
    ).toBe("screenshot");
  });

  test("uses explicit content tabs when their content exists", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "code",
      }),
    ).toBe("code");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "inputs",
        hasInputs: true,
      }),
    ).toBe("inputs");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "outputs",
        hasOutputs: true,
      }),
    ).toBe("outputs");
  });

  test("falls back when an explicit content tab has no content", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "inputs",
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "outputs",
      }),
    ).toBe("screenshot");
  });

  test("keeps explicit recording and run-frame overrides in finalized runs", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "recording",
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        centerView: "screenshot",
        hasRecording: true,
      }),
    ).toBe("screenshot");
  });

  test("prioritizes scrubbed run frames before live or recording defaults", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        scrubbing: true,
        showDebugStream: true,
        recordingOpen: true,
        hasRecording: true,
      }),
    ).toBe("screenshot");
  });

  test("defaults block runs to the live debug stream unless recording is open", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        showDebugStream: true,
        hasRecording: true,
      }),
    ).toBe("stream");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        showDebugStream: true,
        recordingOpen: true,
        hasRecording: true,
      }),
    ).toBe("recording");
  });

  test("uses screenshots while the Browser pane owns a block run debug stream", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        showDebugStream: true,
        debugStreamInBrowserPane: true,
        hasRecording: true,
      }),
    ).toBe("screenshot");
  });

  test("defaults full runs by lifecycle and failure state", () => {
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        running: true,
        hasRecording: true,
      }),
    ).toBe("stream");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        hasRecording: true,
      }),
    ).toBe("recording");
    expect(
      resolveRunHeroCenterView({
        ...defaultArgs,
        failed: true,
        hasRecording: true,
      }),
    ).toBe("screenshot");
  });
});
