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
export const WORKFLOW_TAGGING_FLAG = "WORKFLOW_TAGGING";

// Opt-in (0% base rollout) preview gating the workflows directory-tree view.
// Not enrolled reads as disabled, so the default stays the flat folders/list.
export const WORKFLOWS_DIRECTORY_TREE_FLAG = "WORKFLOWS_DIRECTORY_TREE";

// Opt-in (0% base rollout) preview gating the redesigned workflow studio.
// Not enrolled reads as disabled, so the default stays the legacy editor.
export const WORKFLOW_STUDIO_FLAG = "workflow_studio_v2";
