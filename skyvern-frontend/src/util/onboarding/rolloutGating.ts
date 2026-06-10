import { isABVariant } from "./experimentConfig";

type ActivationOnboarding = {
  isLoading: boolean;
  state: { first_run_at: string | null } | null;
} | null;

// Onboarding surfaces must disappear when the experiment flag is off (0% rollout
// or rollback), so each gate also checks the resolved A/B arm. Without this an
// arm-less cloud user keeps seeing the new surfaces and loses the pre-onboarding
// UI even after rollback.

function isActivationRun(
  flagVariant: string | boolean | undefined,
  onboarding: ActivationOnboarding,
): boolean {
  return (
    isABVariant(flagVariant) &&
    onboarding != null &&
    !onboarding.isLoading &&
    onboarding.state != null &&
    onboarding.state.first_run_at == null
  );
}

function isFirstFailedRunRecoveryEligible(args: {
  flagVariant: string | boolean | undefined;
  isNewUser: boolean;
  isFailureRun: boolean;
  hasFailureReason: boolean;
}): boolean {
  return (
    isABVariant(args.flagVariant) &&
    args.isNewUser &&
    args.isFailureRun &&
    args.hasFailureReason
  );
}

export { isActivationRun, isFirstFailedRunRecoveryEligible };
