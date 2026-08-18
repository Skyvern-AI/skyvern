import { beforeEach, describe, expect, it, vi } from "vitest";
import posthog from "posthog-js";
import {
  getRecoveryGuidanceRetryContext,
  getRecoveryGuidanceRetryNavigation,
  RecoveryGuidanceTelemetry,
  retryRunHasStarted,
  type RecoveryGuidanceTelemetryContext,
} from "./recoveryGuidanceTelemetry";

vi.mock("posthog-js", () => ({
  default: { capture: vi.fn() },
}));

const context: RecoveryGuidanceTelemetryContext = {
  organizationId: "org_opaque",
  experimentVersion: "sky-13471-recovery-guidance-v1",
  arm: "treatment",
  eligibleRunId: "wr_eligible",
  failureCategory: "AUTH_FAILURE",
};

describe("RecoveryGuidanceTelemetry", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("captures the exact shown envelope without a legacy alias", () => {
    RecoveryGuidanceTelemetry.recoveryGuidanceShown(context);
    expect(posthog.capture).toHaveBeenCalledWith("recovery_guidance_shown", {
      organization_id: "org_opaque",
      $groups: { organization: "org_opaque" },
      experiment_version: "sky-13471-recovery-guidance-v1",
      arm: "treatment",
      eligible_run_id: "wr_eligible",
      surface: "runs",
      failure_category: "AUTH_FAILURE",
      $insert_id:
        "recovery_guidance_shown:sky-13471-recovery-guidance-v1:wr_eligible",
    });
    expect(posthog.capture).not.toHaveBeenCalledWith(
      "onboarding.recovery_guidance_shown",
      expect.anything(),
    );
  });

  it("captures an exact clicked event with the selected recovery path", () => {
    RecoveryGuidanceTelemetry.recoveryGuidanceClicked(context, "retry");

    expect(posthog.capture).toHaveBeenCalledWith("recovery_guidance_clicked", {
      organization_id: "org_opaque",
      $groups: { organization: "org_opaque" },
      experiment_version: "sky-13471-recovery-guidance-v1",
      arm: "treatment",
      eligible_run_id: "wr_eligible",
      surface: "runs",
      failure_category: "AUTH_FAILURE",
      path_id: "retry",
    });
  });

  it("links retry-created and retry-started events to the returned retry run", () => {
    RecoveryGuidanceTelemetry.retryCreated(context, "wr_retry");
    RecoveryGuidanceTelemetry.retryStarted(context, "wr_retry");

    expect(posthog.capture).toHaveBeenNthCalledWith(
      1,
      "retry_created",
      expect.objectContaining({
        eligible_run_id: "wr_eligible",
        retry_run_id: "wr_retry",
        path_id: "retry",
        $insert_id:
          "retry_created:sky-13471-recovery-guidance-v1:wr_eligible:wr_retry",
      }),
    );
    expect(posthog.capture).toHaveBeenNthCalledWith(
      2,
      "retry_started",
      expect.objectContaining({
        eligible_run_id: "wr_eligible",
        retry_run_id: "wr_retry",
        path_id: "retry",
        $insert_id:
          "retry_started:sky-13471-recovery-guidance-v1:wr_eligible:wr_retry",
      }),
    );
  });

  it("round-trips the retry button's base state and reserves the full state for the new run", () => {
    const inboundRetryButtonState = {
      data: { retry: true },
      proxyLocation: "RESIDENTIAL",
      webhookCallbackUrl: "",
      maxScreenshotScrolls: null,
      runWith: "agent",
      browserProfileId: null,
      recoveryGuidanceRetry: context,
    };
    const outboundNavigation = { ...context, retryRunId: "wr_retry" };

    expect(getRecoveryGuidanceRetryContext(inboundRetryButtonState)).toEqual(
      context,
    );
    expect(
      getRecoveryGuidanceRetryNavigation(inboundRetryButtonState),
    ).toBeNull();
    expect(
      getRecoveryGuidanceRetryContext({
        recoveryGuidanceRetry: outboundNavigation,
      }),
    ).toBeNull();
    expect(
      getRecoveryGuidanceRetryNavigation({
        recoveryGuidanceRetry: outboundNavigation,
      }),
    ).toEqual(outboundNavigation);
  });

  it("emits retry_started only when the returned retry run has start evidence", () => {
    expect(
      retryRunHasStarted({
        retryRunId: "wr_retry",
        observedRunId: "wr_retry",
        status: "created",
        startedAt: null,
      }),
    ).toBe(false);
    expect(
      retryRunHasStarted({
        retryRunId: "wr_retry",
        observedRunId: "wr_other",
        status: "running",
        startedAt: "2026-08-14T12:00:00Z",
      }),
    ).toBe(false);
    expect(
      retryRunHasStarted({
        retryRunId: "wr_retry",
        observedRunId: "wr_retry",
        status: "running",
        startedAt: null,
      }),
    ).toBe(true);

    const failedBeforeStart = retryRunHasStarted({
      retryRunId: "wr_retry",
      observedRunId: "wr_retry",
      status: "failed",
      startedAt: null,
    });
    if (failedBeforeStart) {
      RecoveryGuidanceTelemetry.retryStarted(context, "wr_retry");
    }
    expect(posthog.capture).not.toHaveBeenCalled();

    const failedAfterStart = retryRunHasStarted({
      retryRunId: "wr_retry",
      observedRunId: "wr_retry",
      status: "failed",
      startedAt: "2026-08-14T12:00:00Z",
    });
    if (failedAfterStart) {
      RecoveryGuidanceTelemetry.retryStarted(context, "wr_retry");
    }
    expect(posthog.capture).toHaveBeenCalledTimes(1);

    vi.clearAllMocks();
    const completedWithoutTimestamp = retryRunHasStarted({
      retryRunId: "wr_retry",
      observedRunId: "wr_retry",
      status: "completed",
      startedAt: null,
    });
    if (completedWithoutTimestamp) {
      RecoveryGuidanceTelemetry.retryStarted(context, "wr_retry");
    }
    expect(posthog.capture).toHaveBeenCalledTimes(1);
  });

  it("drops unbounded category text and never sends failure text or PII properties", () => {
    RecoveryGuidanceTelemetry.recoveryGuidanceShown({
      ...context,
      failureCategory: "email me at person@example.com",
    });

    const properties = vi.mocked(posthog.capture).mock.calls[0]?.[1];
    expect(properties).toEqual({
      organization_id: "org_opaque",
      $groups: { organization: "org_opaque" },
      experiment_version: "sky-13471-recovery-guidance-v1",
      arm: "treatment",
      eligible_run_id: "wr_eligible",
      surface: "runs",
      failure_category: null,
      $insert_id:
        "recovery_guidance_shown:sky-13471-recovery-guidance-v1:wr_eligible",
    });
    expect(Object.keys(properties ?? {})).not.toContain("failure_reason");
    expect(Object.keys(properties ?? {})).not.toContain("email");
  });
});
