import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, fireEvent, cleanup, waitFor } from "@testing-library/react";
import posthog from "posthog-js";
import { FirstRunRecoveryGuidance } from "./FirstRunRecoveryGuidance";
import { getRecoveryPaths } from "./recoveryPaths";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router-dom")>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("posthog-js", () => ({ default: { capture: vi.fn() } }));

const { studioState } = vi.hoisted(() => ({ studioState: { enabled: true } }));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => studioState.enabled,
}));

describe("getRecoveryPaths", () => {
  it("always returns at least two paths (AC1)", () => {
    for (const category of [
      null,
      "",
      "totally_unknown_thing",
      "invalid_credentials",
      "element_not_found",
      "network_error",
    ]) {
      expect(getRecoveryPaths(category).length).toBeGreaterThanOrEqual(2);
    }
  });

  it("selects credential recovery for auth failures (AC2)", () => {
    const ids = getRecoveryPaths("invalid_credentials").map((p) => p.id);
    expect(ids).toContain("update_credentials");
    expect(ids).toContain("retry");
  });

  it("selects workflow editing for element/selector failures (AC2)", () => {
    const ids = getRecoveryPaths("element_not_found").map((p) => p.id);
    expect(ids).toContain("edit_workflow");
    expect(ids).toContain("retry");
  });

  it("selects retry + docs for network failures (AC2)", () => {
    const ids = getRecoveryPaths("network_error").map((p) => p.id);
    expect(ids).toContain("retry");
    expect(ids).toContain("view_docs");
  });

  it("falls back to a generic catalog for unknown categories (AC2 default)", () => {
    const ids = getRecoveryPaths(null).map((p) => p.id);
    expect(ids).toContain("retry");
    expect(ids).toContain("contact_support");
  });
});

describe("FirstRunRecoveryGuidance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    studioState.enabled = true;
  });
  afterEach(() => {
    cleanup();
  });

  it("renders one button per recovery path with at least two actions (AC1)", () => {
    const { getByTestId, container } = render(
      <FirstRunRecoveryGuidance surface="runs" failureCategory={null} />,
    );
    expect(getByTestId("first-run-recovery-guidance")).toBeTruthy();
    const buttons = container.querySelectorAll("button");
    expect(buttons.length).toBeGreaterThanOrEqual(2);
  });

  it("emits the shown event once on mount with the path count (AC3)", () => {
    render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="invalid_credentials"
      />,
    );
    const shownCalls = vi
      .mocked(posthog.capture)
      .mock.calls.filter((c) => c[0] === "onboarding.recovery_guidance_shown");
    expect(shownCalls).toHaveLength(1);
    expect(shownCalls[0]?.[1]).toMatchObject({
      surface: "runs",
      failure_category: "invalid_credentials",
      path_count: 2,
    });
  });

  it("records chosen + navigated outcome for a navigate path (AC3)", () => {
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="invalid_credentials"
        workflowPermanentId="wpid_123"
      />,
    );
    fireEvent.click(getByTestId("recovery-path-update_credentials"));
    expect(navigateMock).toHaveBeenCalledWith("/credentials");
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_path_chosen",
      expect.objectContaining({ path_id: "update_credentials" }),
    );
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_outcome",
      expect.objectContaining({
        path_id: "update_credentials",
        outcome: "navigated",
      }),
    );
  });

  it("navigates to the workflow editor with the permanent id (AC2 wiring)", () => {
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="element_not_found"
        workflowPermanentId="wpid_123"
      />,
    );
    fireEvent.click(getByTestId("recovery-path-edit_workflow"));
    expect(navigateMock).toHaveBeenCalledWith("/agents/wpid_123/studio");
  });

  it("navigates to /build when the studio preview is off (AC2 wiring)", () => {
    studioState.enabled = false;
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="element_not_found"
        workflowPermanentId="wpid_123"
      />,
    );
    fireEvent.click(getByTestId("recovery-path-edit_workflow"));
    expect(navigateMock).toHaveBeenCalledWith("/agents/wpid_123/build");
  });

  it("records chosen + opened outcome for an external path (AC3)", () => {
    const openSpy = vi.spyOn(window, "open").mockImplementation(() => null);
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="network_error"
      />,
    );
    fireEvent.click(getByTestId("recovery-path-view_docs"));
    expect(openSpy).toHaveBeenCalled();
    expect(posthog.capture).toHaveBeenCalledWith(
      "onboarding.recovery_outcome",
      expect.objectContaining({ path_id: "view_docs", outcome: "opened" }),
    );
    openSpy.mockRestore();
  });

  it("records retry_started when the retry handler resolves (AC3)", async () => {
    const onRetry = vi.fn().mockResolvedValue(undefined);
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="network_error"
        onRetry={onRetry}
      />,
    );
    fireEvent.click(getByTestId("recovery-path-retry"));
    expect(onRetry).toHaveBeenCalled();
    await waitFor(() => {
      expect(posthog.capture).toHaveBeenCalledWith(
        "onboarding.recovery_outcome",
        expect.objectContaining({ path_id: "retry", outcome: "retry_started" }),
      );
    });
  });

  it("records retry_failed_to_start when the retry handler rejects (AC3)", async () => {
    const onRetry = vi.fn().mockRejectedValue(new Error("boom"));
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        surface="runs"
        failureCategory="network_error"
        onRetry={onRetry}
      />,
    );
    fireEvent.click(getByTestId("recovery-path-retry"));
    await waitFor(() => {
      expect(posthog.capture).toHaveBeenCalledWith(
        "onboarding.recovery_outcome",
        expect.objectContaining({
          path_id: "retry",
          outcome: "retry_failed_to_start",
        }),
      );
    });
  });
});
