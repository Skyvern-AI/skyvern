import { describe, expect, it } from "vitest";

import { resolveDrainAction, resolveSendAction } from "./sendQueue";

const sendInput = (
  overrides: Partial<Parameters<typeof resolveSendAction>[0]> = {},
): Parameters<typeof resolveSendAction>[0] => ({
  inFlight: false,
  hasQueuedPrompt: false,
  requiresLiveBrowser: false,
  isLiveBrowserReady: false,
  candidate: "hello",
  isDrain: false,
  skipQueue: false,
  ...overrides,
});

describe("resolveSendAction", () => {
  it("returns noop for an empty candidate", () => {
    expect(resolveSendAction(sendInput({ candidate: "   " }))).toBe("noop");
  });

  it("returns noop when a prompt is already queued and this is not a drain", () => {
    expect(
      resolveSendAction(sendInput({ hasQueuedPrompt: true, isDrain: false })),
    ).toBe("noop");
  });

  it("queues for the next turn while a turn is in flight", () => {
    expect(resolveSendAction(sendInput({ inFlight: true }))).toBe(
      "queue_working",
    );
  });

  it("prioritizes the in-flight queue over the live-browser queue", () => {
    expect(
      resolveSendAction(
        sendInput({
          inFlight: true,
          requiresLiveBrowser: true,
          isLiveBrowserReady: false,
        }),
      ),
    ).toBe("queue_working");
  });

  it("queues for the live browser when one is required but not ready", () => {
    expect(
      resolveSendAction(
        sendInput({ requiresLiveBrowser: true, isLiveBrowserReady: false }),
      ),
    ).toBe("queue_live_browser");
  });

  it("sends when the required live browser is ready", () => {
    expect(
      resolveSendAction(
        sendInput({ requiresLiveBrowser: true, isLiveBrowserReady: true }),
      ),
    ).toBe("send");
  });

  it("sends when no live browser is required", () => {
    expect(resolveSendAction(sendInput())).toBe("send");
  });

  it("bypasses the single-queue guard while draining", () => {
    expect(
      resolveSendAction(sendInput({ hasQueuedPrompt: true, isDrain: true })),
    ).toBe("send");
  });

  it("skipQueue forces a send past the live-browser predicate", () => {
    expect(
      resolveSendAction(
        sendInput({
          isDrain: true,
          skipQueue: true,
          requiresLiveBrowser: true,
          isLiveBrowserReady: false,
        }),
      ),
    ).toBe("send");
  });
});

const drainInput = (
  overrides: Partial<Parameters<typeof resolveDrainAction>[0]> = {},
): Parameters<typeof resolveDrainAction>[0] => ({
  queuedReason: null,
  inFlight: false,
  hasLiveBrowserSession: false,
  hasWorkflowPermanentId: true,
  ...overrides,
});

describe("resolveDrainAction", () => {
  it("waits when nothing is queued", () => {
    expect(resolveDrainAction(drainInput({ queuedReason: null }))).toBe("wait");
  });

  it("waits while a turn is in flight", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", inFlight: true }),
      ),
    ).toBe("wait");
  });

  it("waits without a workflow permanent id", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasWorkflowPermanentId: false }),
      ),
    ).toBe("wait");
  });

  it("re-queues a working prompt regardless of browser readiness", () => {
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasLiveBrowserSession: false }),
      ),
    ).toBe("drain_requeue");
    expect(
      resolveDrainAction(
        drainInput({ queuedReason: "working", hasLiveBrowserSession: true }),
      ),
    ).toBe("drain_requeue");
  });

  it("skip-queue sends a live_browser prompt only once the session exists", () => {
    expect(
      resolveDrainAction(
        drainInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: true,
        }),
      ),
    ).toBe("drain_skip_queue");
  });

  it("waits for the session before draining a live_browser prompt", () => {
    expect(
      resolveDrainAction(
        drainInput({
          queuedReason: "live_browser",
          hasLiveBrowserSession: false,
        }),
      ),
    ).toBe("wait");
  });
});
