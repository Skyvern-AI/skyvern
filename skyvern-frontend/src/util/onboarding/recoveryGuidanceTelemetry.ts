import posthog from "posthog-js";
import type { RecoveryPathId } from "@/components/onboarding/recoveryPaths";

type Surface = "runs";

type RecoveryGuidanceTelemetryContext = {
  organizationId: string;
  experimentVersion: string;
  arm: "control" | "treatment";
  eligibleRunId: string;
  failureCategory: string | null;
};

type RecoveryGuidanceRetryNavigation = RecoveryGuidanceTelemetryContext & {
  retryRunId: string;
};

const FAILURE_CATEGORY_PATTERN = /^[A-Z][A-Z0-9_]{0,63}$/;
const STARTED_RETRY_STATUSES: Record<string, true> = {
  running: true,
  completed: true,
};

function capture(event: string, properties: Record<string, unknown>): void {
  try {
    posthog.capture(event, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

function boundedFailureCategory(value: string | null): string | null {
  return value && FAILURE_CATEGORY_PATTERN.test(value) ? value : null;
}

function commonProperties(
  context: RecoveryGuidanceTelemetryContext,
): Record<string, unknown> {
  return {
    organization_id: context.organizationId,
    $groups: { organization: context.organizationId },
    experiment_version: context.experimentVersion,
    arm: context.arm,
    eligible_run_id: context.eligibleRunId,
    surface: "runs",
    failure_category: boundedFailureCategory(context.failureCategory),
  };
}

function insertId(
  event: string,
  context: RecoveryGuidanceTelemetryContext,
  suffix?: string,
): string {
  return [event, context.experimentVersion, context.eligibleRunId, suffix]
    .filter((value): value is string => Boolean(value))
    .join(":");
}

function recoveryGuidanceShown(
  context: RecoveryGuidanceTelemetryContext,
): void {
  capture("recovery_guidance_shown", {
    ...commonProperties(context),
    $insert_id: insertId("recovery_guidance_shown", context),
  });
}

function recoveryGuidanceClicked(
  context: RecoveryGuidanceTelemetryContext,
  pathId: RecoveryPathId,
): void {
  capture("recovery_guidance_clicked", {
    ...commonProperties(context),
    path_id: pathId,
  });
}

function retryCreated(
  context: RecoveryGuidanceTelemetryContext,
  retryRunId: string,
): void {
  capture("retry_created", {
    ...commonProperties(context),
    retry_run_id: retryRunId,
    path_id: "retry",
    $insert_id: insertId("retry_created", context, retryRunId),
  });
}

function retryStarted(
  context: RecoveryGuidanceTelemetryContext,
  retryRunId: string,
): void {
  capture("retry_started", {
    ...commonProperties(context),
    retry_run_id: retryRunId,
    path_id: "retry",
    $insert_id: insertId("retry_started", context, retryRunId),
  });
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function hasRecoveryGuidanceTelemetryContext(
  value: unknown,
): value is RecoveryGuidanceTelemetryContext {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<RecoveryGuidanceTelemetryContext>;
  return (
    isNonEmptyString(candidate.organizationId) &&
    isNonEmptyString(candidate.experimentVersion) &&
    (candidate.arm === "control" || candidate.arm === "treatment") &&
    isNonEmptyString(candidate.eligibleRunId) &&
    (candidate.failureCategory === null ||
      (typeof candidate.failureCategory === "string" &&
        boundedFailureCategory(candidate.failureCategory) !== null))
  );
}

function isRecoveryGuidanceTelemetryContext(
  value: unknown,
): value is RecoveryGuidanceTelemetryContext {
  return hasRecoveryGuidanceTelemetryContext(value) && !("retryRunId" in value);
}

function isRecoveryGuidanceRetryNavigation(
  value: unknown,
): value is RecoveryGuidanceRetryNavigation {
  return (
    hasRecoveryGuidanceTelemetryContext(value) &&
    isNonEmptyString(
      (value as Partial<RecoveryGuidanceRetryNavigation>).retryRunId,
    )
  );
}

function getRecoveryGuidanceRetryContext(
  locationState: unknown,
): RecoveryGuidanceTelemetryContext | null {
  if (!locationState || typeof locationState !== "object") {
    return null;
  }
  const candidate = locationState as {
    recoveryGuidanceRetry?: unknown;
  };
  return isRecoveryGuidanceTelemetryContext(candidate.recoveryGuidanceRetry)
    ? candidate.recoveryGuidanceRetry
    : null;
}

function getRecoveryGuidanceRetryNavigation(
  locationState: unknown,
): RecoveryGuidanceRetryNavigation | null {
  if (!locationState || typeof locationState !== "object") {
    return null;
  }
  const candidate = locationState as {
    recoveryGuidanceRetry?: unknown;
  };
  return isRecoveryGuidanceRetryNavigation(candidate.recoveryGuidanceRetry)
    ? candidate.recoveryGuidanceRetry
    : null;
}

function retryRunHasStarted({
  retryRunId,
  observedRunId,
  status,
  startedAt,
}: {
  retryRunId: string;
  observedRunId: string | undefined;
  status: string | undefined;
  startedAt: string | null | undefined;
}): boolean {
  return (
    retryRunId === observedRunId &&
    (startedAt !== null && startedAt !== undefined
      ? true
      : status !== undefined && status in STARTED_RETRY_STATUSES)
  );
}

export const RecoveryGuidanceTelemetry = {
  recoveryGuidanceShown,
  recoveryGuidanceClicked,
  retryCreated,
  retryStarted,
} as const;

export {
  getRecoveryGuidanceRetryContext,
  getRecoveryGuidanceRetryNavigation,
  isRecoveryGuidanceTelemetryContext,
  isRecoveryGuidanceRetryNavigation,
  retryRunHasStarted,
};
export type {
  RecoveryGuidanceRetryNavigation,
  RecoveryGuidanceTelemetryContext,
  Surface,
};
