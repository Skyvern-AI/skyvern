export const ANALYTICS_DASHBOARD_FLAG = "ANALYTICS_DASHBOARD";

// Gates the anomaly alert feed on /analytics; the backend /analytics/anomalies
// route enforces the same flag server-side.
export const ANALYTICS_ANOMALY_DETECTION_FLAG = "ANALYTICS_ANOMALY_DETECTION";

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

// Gates the optional, server-confirmed onboarding details step.
export const ONBOARDING_QUESTIONNAIRE_FLAG = "onboarding_questionnaire_v1";

// Gates the login-block fallback-credential editor. Off ⇒ the fallback config is hidden, because
// automatic retries only run for orgs in the CREDENTIAL_FALLBACK_RETRY rollout (backend gate), so
// showing the editor to other orgs would promise a retry that never fires. Server-evaluated via
// /customer (see FeatureFlagProvider), keyed on organization_id like the backend gate.
export const CREDENTIAL_FALLBACK_RETRY_FLAG = "CREDENTIAL_FALLBACK_RETRY";

// Opt-in preview (0% base): recordings synthesize code blocks instead of agent blocks.
export const RECORD_BROWSER_CODE_FIRST_FLAG = "record_browser_code_first";

export const ONBOARDING_PROGRESS_FLAG = "onboarding_progress_v1";

export const ONBOARDING_TRACK_FLAG = "onboarding_track_v1";

// Gates the second-agent track row and retires the standalone credit card.
export const ONBOARDING_TRACK_SECOND_AGENT_FLAG =
  "onboarding_track_second_agent_v1";
