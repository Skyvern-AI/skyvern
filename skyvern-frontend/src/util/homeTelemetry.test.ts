import { afterEach, describe, expect, it, vi } from "vitest";

const { capture } = vi.hoisted(() => ({ capture: vi.fn() }));

vi.mock("posthog-js", () => ({
  default: { capture },
}));

import { HomeTelemetry } from "./homeTelemetry";

afterEach(() => {
  capture.mockReset();
  vi.restoreAllMocks();
});

describe("HomeTelemetry agent creation", () => {
  it("uses one privacy-safe attempt id across submitted and succeeded events", () => {
    const attempt = HomeTelemetry.agentCreationSubmitted({
      source: "typed",
      handoff: false,
      variant: "revamp",
    });

    HomeTelemetry.promptSubmitted({
      attemptId: attempt.attemptId,
      source: "typed",
      promptLength: 18,
      handoff: false,
    });
    HomeTelemetry.agentCreationSucceeded(attempt, "wpid_created");

    expect(attempt.attemptId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
    expect(capture).toHaveBeenNthCalledWith(
      1,
      "home.agent_creation_submitted",
      {
        attempt_id: attempt.attemptId,
        source: "typed",
        handoff: false,
        variant: "revamp",
      },
    );
    expect(capture).toHaveBeenNthCalledWith(2, "home.prompt_submitted", {
      attempt_id: attempt.attemptId,
      source: "typed",
      promptLength: 18,
      handoff: false,
    });
    expect(capture).toHaveBeenNthCalledWith(
      3,
      "home.agent_creation_succeeded",
      {
        attempt_id: attempt.attemptId,
        source: "typed",
        handoff: false,
        variant: "revamp",
        workflow_permanent_id: "wpid_created",
      },
    );
    expect(capture.mock.calls.flat()).not.toContain("Visit private site");
  });

  it("generates a fresh attempt id for every submission", () => {
    const first = HomeTelemetry.agentCreationSubmitted({
      source: "typed",
      handoff: true,
      variant: "legacy",
    });
    const retry = HomeTelemetry.agentCreationSubmitted({
      source: "typed",
      handoff: true,
      variant: "legacy",
    });

    expect(retry.attemptId).not.toBe(first.attemptId);
  });

  it("falls back when randomUUID is unavailable", () => {
    vi.spyOn(globalThis.crypto, "randomUUID").mockImplementation(() => {
      throw new Error("randomUUID unavailable");
    });

    const attempt = HomeTelemetry.agentCreationSubmitted({
      source: "typed",
      handoff: false,
      variant: "legacy",
    });

    expect(attempt.attemptId).toBeTruthy();
    expect(capture).toHaveBeenCalledWith(
      "home.agent_creation_submitted",
      expect.objectContaining({ attempt_id: attempt.attemptId }),
    );
  });

  it.each([
    [{ isAxiosError: true, response: { status: 402 } }, "payment_required"],
    [{ isAxiosError: true, response: { status: 422 } }, "invalid_request"],
    [{ isAxiosError: true, response: { status: 429 } }, "rate_limited"],
    [{ isAxiosError: true, response: { status: 503 } }, "server_error"],
    [{ isAxiosError: true }, "network_error"],
    [new Error("unexpected response containing private data"), "client_error"],
  ])(
    "normalizes failures without capturing error details",
    (error, category) => {
      const attempt = HomeTelemetry.agentCreationSubmitted({
        source: "example",
        example: "sample_key",
        handoff: false,
        variant: "legacy",
      });
      capture.mockClear();

      HomeTelemetry.agentCreationFailed(attempt, error);

      expect(capture).toHaveBeenCalledWith("home.agent_creation_failed", {
        attempt_id: attempt.attemptId,
        source: "example",
        example: "sample_key",
        handoff: false,
        variant: "legacy",
        error_category: category,
      });
      expect(JSON.stringify(capture.mock.calls)).not.toContain("private data");
    },
  );
});
