import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";
import posthog from "posthog-js";
import type * as ReactRouterDom from "react-router-dom";
import {
  FirstRunRecoveryGuidance,
  shouldShowRecoveryGuidance,
} from "./FirstRunRecoveryGuidance";
import { getRecoveryPaths } from "./recoveryPaths";
import type { RecoveryGuidanceTelemetryContext } from "@/util/onboarding/recoveryGuidanceTelemetry";

const navigateMock = vi.fn();
vi.mock("react-router-dom", async (importOriginal) => {
  const actual = await importOriginal<typeof ReactRouterDom>();
  return { ...actual, useNavigate: () => navigateMock };
});

vi.mock("posthog-js", () => ({ default: { capture: vi.fn() } }));

const { studioState } = vi.hoisted(() => ({ studioState: { enabled: true } }));
vi.mock("@/hooks/useWorkflowStudioEnabled", () => ({
  useWorkflowStudioEnabled: () => studioState.enabled,
}));

const telemetryContext: RecoveryGuidanceTelemetryContext = {
  organizationId: "org_opaque",
  experimentVersion: "sky-13471-recovery-guidance-v1",
  arm: "treatment",
  eligibleRunId: "wr_failure",
  failureCategory: "AUTH_FAILURE",
};

describe("getRecoveryPaths", () => {
  it("always returns at least two paths", () => {
    for (const category of [
      null,
      "",
      "totally_unknown_thing",
      "AUTH_FAILURE",
      "ELEMENT_NOT_FOUND",
      "PAGE_LOAD_TIMEOUT",
    ]) {
      expect(getRecoveryPaths(category).length).toBeGreaterThanOrEqual(2);
    }
  });
});

describe("recovery guidance dormant gate", () => {
  it("keeps the treatment surface invisible until its dedicated flag is explicitly enabled", () => {
    const assignment = {
      experiment_version: "sky-13471-recovery-guidance-v1",
      organization_id: "org_opaque",
      eligible_run_id: "wr_failure",
      eligible_at: "2026-08-14T12:00:00Z",
      arm: "treatment" as const,
    };

    expect(
      shouldShowRecoveryGuidance({
        assignment,
        workflowRunId: "wr_failure",
        treatmentSurfaceEnabled: false,
      }),
    ).toBe(false);
    expect(
      shouldShowRecoveryGuidance({
        assignment: { ...assignment, arm: "control" as const },
        workflowRunId: "wr_failure",
        treatmentSurfaceEnabled: true,
      }),
    ).toBe(false);
  });
});

describe("FirstRunRecoveryGuidance", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    studioState.enabled = true;
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
  });

  it("emits the exact shown event once when the dormant component is mounted", () => {
    render(<FirstRunRecoveryGuidance telemetryContext={telemetryContext} />);

    expect(posthog.capture).toHaveBeenCalledTimes(1);
    expect(posthog.capture).toHaveBeenCalledWith(
      "recovery_guidance_shown",
      expect.objectContaining({
        organization_id: "org_opaque",
        eligible_run_id: "wr_failure",
        surface: "runs",
        failure_category: "AUTH_FAILURE",
      }),
    );
  });

  it("records an exact click but does not call retry_started on form navigation", () => {
    const onRetry = vi.fn();
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        telemetryContext={telemetryContext}
        onRetry={onRetry}
      />,
    );

    fireEvent.click(getByTestId("recovery-path-retry"));

    expect(onRetry).toHaveBeenCalledOnce();
    expect(posthog.capture).toHaveBeenCalledWith(
      "recovery_guidance_clicked",
      expect.objectContaining({
        eligible_run_id: "wr_failure",
        path_id: "retry",
      }),
    );
    expect(posthog.capture).not.toHaveBeenCalledWith(
      "retry_started",
      expect.anything(),
    );
  });

  it("keeps workflow-editor navigation wired to the selected path", () => {
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        telemetryContext={{
          ...telemetryContext,
          failureCategory: "ELEMENT_NOT_FOUND",
        }}
        workflowPermanentId="wpid_123"
      />,
    );

    fireEvent.click(getByTestId("recovery-path-edit_workflow"));

    expect(navigateMock).toHaveBeenCalledWith("/agents/wpid_123/studio");
    expect(posthog.capture).toHaveBeenCalledWith(
      "recovery_guidance_clicked",
      expect.objectContaining({ path_id: "edit_workflow" }),
    );
  });

  it("falls back to the agents page when edit-workflow has no workflow id", () => {
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        telemetryContext={{
          ...telemetryContext,
          failureCategory: "ELEMENT_NOT_FOUND",
        }}
      />,
    );

    fireEvent.click(getByTestId("recovery-path-edit_workflow"));

    expect(navigateMock).toHaveBeenCalledWith("/agents");
  });

  it("opens external recovery links without an opener or referrer", () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    const { getByTestId } = render(
      <FirstRunRecoveryGuidance
        telemetryContext={{
          ...telemetryContext,
          failureCategory: "PAGE_LOAD_TIMEOUT",
        }}
      />,
    );

    fireEvent.click(getByTestId("recovery-path-view_docs"));

    expect(open).toHaveBeenCalledWith(
      "https://docs.skyvern.com",
      "_blank",
      "noopener,noreferrer",
    );
  });
});
