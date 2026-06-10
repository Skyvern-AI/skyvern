export const ANALYTICS_DASHBOARD_FLAG = "ANALYTICS_DASHBOARD";

/**
 * Gates the workflow editor onboarding tour and A/B experiment.
 * When disabled, users skip the tour and experiment routing entirely.
 *
 * Ramp plan (see rolloutConfig.ts for structured constants):
 *   0%   - ship day
 *   10%  - hold 1 day, gate check
 *   50%  - hold 3 days, gate check
 *   100% - GA
 *
 * PostHog type: multivariate string flag. Control / not-enrolled reads as no
 * variant; enabled arms are "template-first" | "copilot-first". Onboarding
 * surfaces gate on a resolved variant (isABVariant), so 0% or rollback hides them.
 */
export const EDITOR_ONBOARDING_TOUR_FLAG = "EDITOR_ONBOARDING_TOUR";
