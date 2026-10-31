import { describe, expect, it } from "vitest";

import { browserIsLive } from "./browserLiveStatus";

describe("browserIsLive", () => {
  const empty = {
    liveSurface: "debug" as const,
    debugBrowserSessionId: null,
    debugStream: null,
    runId: undefined,
    runStream: null,
  };

  it("does not treat an absent browser session as live", () => {
    expect(browserIsLive(empty)).toBe(false);
  });

  it("is live only when the displayed run stream is live", () => {
    expect(
      browserIsLive({
        ...empty,
        liveSurface: "run",
        runId: "run-current",
        runStream: { workflowRunId: "run-current", state: "live" },
      }),
    ).toBe(true);
    expect(
      browserIsLive({
        ...empty,
        liveSurface: "run",
        runId: "run-current",
        runStream: { workflowRunId: "run-finished", state: "live" },
      }),
    ).toBe(false);
    expect(
      browserIsLive({
        ...empty,
        liveSurface: "run",
        runId: "run-finished",
        runStream: { workflowRunId: "run-finished", state: "stopped" },
      }),
    ).toBe(false);
  });

  it("is live in edit context when the current debug browser is streaming", () => {
    expect(
      browserIsLive({
        ...empty,
        debugBrowserSessionId: "browser-current",
        debugStream: { browserSessionId: "browser-current", state: "live" },
      }),
    ).toBe(true);
    expect(
      browserIsLive({
        ...empty,
        debugBrowserSessionId: "browser-current",
        debugStream: { browserSessionId: "browser-old", state: "live" },
      }),
    ).toBe(false);
  });
});
