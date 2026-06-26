import posthog from "posthog-js";

type Surface = "dashboard" | "editor" | "runs" | "settings" | "api_docs";

type RecoveryOutcome =
  | "navigated"
  | "opened"
  | "retry_started"
  | "retry_failed_to_start";

function capture(event: string, properties: Record<string, unknown>): void {
  try {
    posthog.capture(event, properties);
  } catch {
    // PostHog may be unavailable in tests or before init.
  }
}

function recoveryGuidanceShown(
  surface: Surface,
  failureCategory: string | null,
  pathCount: number,
): void {
  capture("onboarding.recovery_guidance_shown", {
    surface,
    failure_category: failureCategory,
    path_count: pathCount,
  });
}

function recoveryPathChosen(
  surface: Surface,
  failureCategory: string | null,
  pathId: string,
): void {
  capture("onboarding.recovery_path_chosen", {
    surface,
    failure_category: failureCategory,
    path_id: pathId,
  });
}

function recoveryOutcome(
  surface: Surface,
  failureCategory: string | null,
  pathId: string,
  outcome: RecoveryOutcome,
): void {
  capture("onboarding.recovery_outcome", {
    surface,
    failure_category: failureCategory,
    path_id: pathId,
    outcome,
  });
}

export const RecoveryGuidanceTelemetry = {
  recoveryGuidanceShown,
  recoveryPathChosen,
  recoveryOutcome,
} as const;

export type { Surface, RecoveryOutcome };
