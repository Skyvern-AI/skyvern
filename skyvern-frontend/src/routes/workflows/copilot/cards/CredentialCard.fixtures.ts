import type {
  CredentialPauseHistorical,
  CredentialRequiredFrame,
  MatchingCredential,
} from "./CredentialCard";

export function buildCredentialRequiredFrame(
  overrides: Partial<CredentialRequiredFrame> = {},
): CredentialRequiredFrame {
  return {
    type: "credential_required",
    turn_id: "turn_123",
    workflow_copilot_chat_id: "wcc_123",
    reason: "workflow_credential_inputs_unbound",
    message:
      "The draft is ready, but news.ycombinator.com needs a login before I can test-run it.",
    login_page_urls: ["https://news.ycombinator.com/login"],
    credential_refs: [],
    timeout_seconds: 300,
    expires_at: new Date(Date.now() + 300_000).toISOString(),
    timestamp: new Date().toISOString(),
    ...overrides,
  };
}

export const CREDENTIAL_REQUIRED_FRAME_BY_REASON = {
  workflow_credential_inputs_unbound: buildCredentialRequiredFrame({
    reason: "workflow_credential_inputs_unbound",
  }),
  credential_name_unresolved: buildCredentialRequiredFrame({
    reason: "credential_name_unresolved",
    message: undefined,
  }),
  credential_invention_requested: buildCredentialRequiredFrame({
    reason: "credential_invention_requested",
    message: undefined,
  }),
  raw_secret: buildCredentialRequiredFrame({
    reason: "raw_secret",
    message: undefined,
  }),
  assistant_directed: buildCredentialRequiredFrame({
    reason: "assistant_directed",
    message: undefined,
  }),
  missing_credential_run_failure: buildCredentialRequiredFrame({
    reason: "missing_credential_run_failure",
    message: "The last run stopped at the Hacker News login step.",
  }),
  credential_deferred_draft: buildCredentialRequiredFrame({
    reason: "credential_deferred_draft",
    message: "Picking back up on the credential you held off on earlier.",
  }),
} as const;

// No dynamic ask text; only a richer, timed pause signal supplies one.
export const CREDENTIAL_REQUIRED_FRAME_NO_MESSAGE: CredentialRequiredFrame =
  buildCredentialRequiredFrame({
    reason: "credential_name_unresolved",
    message: undefined,
  });

// Mirrors a minimal request-policy-time classifier signal exactly: only
// `type` and `reason` are ever guaranteed, everything else is undefined.
export const CREDENTIAL_REQUIRED_FRAME_MINIMAL: CredentialRequiredFrame = {
  type: "credential_required",
  reason: "workflow_credential_inputs_unbound",
};

export const NO_MATCHING_CREDENTIALS: MatchingCredential[] = [];

export const ONE_MATCHING_CREDENTIAL: MatchingCredential[] = [
  { credentialId: "cred_hn", name: "HN login" },
];

export const TWO_MATCHING_CREDENTIALS: MatchingCredential[] = [
  { credentialId: "cred_hn", name: "HN login" },
  { credentialId: "cred_acme", name: "Acme portal" },
];

export const MANY_MATCHING_CREDENTIALS: MatchingCredential[] = [
  { credentialId: "cred_hn", name: "HN login" },
  { credentialId: "cred_acme", name: "Acme portal" },
  { credentialId: "cred_qa", name: "QA test account" },
];

export const RESOLVED_OUTCOME_CONNECTED: CredentialPauseHistorical = {
  outcome: "connected",
  credentialId: "cred_hn",
};

export const RESOLVED_OUTCOME_SKIPPED: CredentialPauseHistorical = {
  outcome: "skipped",
};

export const RESOLVED_OUTCOME_TIMEOUT: CredentialPauseHistorical = {
  outcome: "timeout",
};
