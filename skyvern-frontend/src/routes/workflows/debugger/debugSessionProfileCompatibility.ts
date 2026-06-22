import type {
  CredentialApiResponse,
  DebugLoginBlockCompatibilityResponse,
} from "@/api/types";

export const DEBUG_LOGIN_GATE_CREDENTIALS_PAGE_SIZE = 200;

export type DebugSessionProfileIncompatibilityReason =
  | "pbs_no_profile"
  | "pbs_different_profile";

export type DebugSessionProfileCompatibilityResult = {
  compatible: boolean;
  reason: DebugSessionProfileIncompatibilityReason | null;
};

export type CredentialsLoadState = "loading" | "ready" | "error";

/**
 * Outcome of resolving the LoginBlock's credential reference against the
 * currently fetched credentials list.
 *
 * - `no-credential-parameter`: the block has no credential parameter at all,
 *   so there is no saved profile to enforce.
 * - `resolved`: the credential was found in the list. `browserProfileId` is
 *   its saved profile, or `null` if no profile is attached.
 * - `credential-not-in-list`: the block references a credential by id but
 *   that id is not in the fetched list (paginated past the window, deleted,
 *   inaccessible, etc.). The gate must not collapse this into "no profile"
 *   — it has to fail closed and let the caller refetch.
 */
export type CredentialResolution =
  | { status: "no-credential-parameter" }
  | { status: "resolved"; browserProfileId: string | null }
  | { status: "credential-not-in-list"; credentialId: string };

export type DebugLoginPlayGateRetryReason =
  | "credentials-error"
  | "credential-not-found";

export type DebugLoginPlayGateAction =
  | { kind: "proceed" }
  | { kind: "block-loading" }
  | { kind: "block-retry"; reason: DebugLoginPlayGateRetryReason }
  | { kind: "show-modal"; reason: DebugSessionProfileIncompatibilityReason };

type BlockLike = {
  block_type?: string | null;
  label?: string;
  parameters?: ReadonlyArray<unknown> | null;
};

type ParameterLike = {
  parameter_type?: string | null;
  credential_id?: string | null;
  workflow_parameter_type?: string | null;
  default_value?: unknown;
};

/**
 * Mirrors backend `_resolve_login_block_browser_profile_id` for the FE
 * pre-check. Walks the LoginBlock's parameters, picks the first credential
 * reference, and reports whether that credential is present in the supplied
 * list and what its saved `browser_profile_id` is.
 *
 * Two reference styles are recognized, matching the backend resolver:
 *   1. CredentialParameter — direct `credential_id`.
 *   2. WorkflowParameter with `workflow_parameter_type === "credential_id"` —
 *      run-time supplies the id; pre-run we use `default_value` (the same
 *      fallback the backend resolver takes when no run params exist).
 *
 * The expressive return type is load-bearing: `credential-not-in-list` is
 * what stops the loading-page bypass — a credential whose id never appears
 * in the fetched window, or a Style-2 reference whose `default_value` we
 * cannot resolve locally, must not be silently treated as "no profile".
 */
export function resolveLoginBlockCredential(args: {
  block: BlockLike | null | undefined;
  credentials: ReadonlyArray<CredentialApiResponse> | null | undefined;
}): CredentialResolution {
  const { block, credentials } = args;
  if (!block || block.block_type !== "login") {
    return { status: "no-credential-parameter" };
  }
  const parameters = block.parameters ?? [];

  let referencedCredentialId: string | null = null;
  let sawUnresolvableCredentialReference = false;
  for (const rawParam of parameters) {
    const param = rawParam as ParameterLike;
    if (param?.parameter_type === "credential") {
      const credentialId = param.credential_id ?? null;
      if (!credentialId) continue;
      referencedCredentialId = credentialId;
      break;
    }
    if (
      param?.parameter_type === "workflow" &&
      param?.workflow_parameter_type === "credential_id"
    ) {
      const dv = param.default_value;
      const credentialId = typeof dv === "string" && dv ? dv : null;
      if (!credentialId) {
        sawUnresolvableCredentialReference = true;
        continue;
      }
      referencedCredentialId = credentialId;
      break;
    }
  }

  if (referencedCredentialId === null) {
    if (sawUnresolvableCredentialReference) {
      return {
        status: "credential-not-in-list",
        credentialId: "",
      };
    }
    return { status: "no-credential-parameter" };
  }

  const list = credentials ?? [];
  const credential = list.find(
    (c) => c.credential_id === referencedCredentialId,
  );
  if (!credential) {
    return {
      status: "credential-not-in-list",
      credentialId: referencedCredentialId,
    };
  }
  return {
    status: "resolved",
    browserProfileId: credential.browser_profile_id ?? null,
  };
}

/**
 * Mirrors backend `_evaluate_debug_session_profile_decision`.
 *
 * The visible PBS only "matches" the credential when both saved profiles are
 * identical. Any mismatch means running the LoginBlock through the credential's
 * saved profile would launch a fresh browser the user can't see — exactly the
 * stream divergence customers reported. The modal that consumes this verdict
 * is informational; the backend still enforces the asymmetric behavior on
 * the run path.
 */
export function evaluateDebugSessionProfileCompatibility(args: {
  pbsBrowserProfileId: string | null;
  credentialBrowserProfileId: string | null;
}): DebugSessionProfileCompatibilityResult {
  const { pbsBrowserProfileId, credentialBrowserProfileId } = args;

  if (!credentialBrowserProfileId) {
    return { compatible: true, reason: null };
  }
  if (!pbsBrowserProfileId) {
    return { compatible: false, reason: "pbs_no_profile" };
  }
  if (pbsBrowserProfileId !== credentialBrowserProfileId) {
    return { compatible: false, reason: "pbs_different_profile" };
  }
  return { compatible: true, reason: null };
}

/**
 * Decide what the debugger Play button should do for a (potentially) LoginBlock
 * debug run. Fail-closed when the data we need is not in hand: the gate
 * distinguishes the initial loading state from a recoverable error/missing-
 * credential state so the caller can choose the right toast and trigger a
 * refetch where appropriate.
 *
 * - `proceed`: gate doesn't apply or the resolved credential is compatible
 *   with the visible PBS.
 * - `block-loading`: credentials query is still in flight; caller should
 *   surface a "try again in a moment" toast.
 * - `block-retry`: credentials errored or the LoginBlock's credential isn't
 *   in the fetched list; caller should `refetch()` and surface a retry-
 *   specific toast. `reason` lets the caller pick copy and decide whether
 *   to widen the page on retry.
 * - `show-modal`: credentials are ready and the visible PBS profile would
 *   diverge from the credential profile; surface the modal so the user
 *   can Continue (proceed anyway) or Cancel.
 */
export function decideDebugLoginPlayGate(args: {
  blockType: string;
  hasDebugSession: boolean;
  credentialsState: CredentialsLoadState;
  block: BlockLike | null | undefined;
  credentials: ReadonlyArray<CredentialApiResponse> | null | undefined;
  pbsBrowserProfileId: string | null;
}): DebugLoginPlayGateAction {
  const {
    blockType,
    hasDebugSession,
    credentialsState,
    block,
    credentials,
    pbsBrowserProfileId,
  } = args;

  if (blockType !== "login" || !hasDebugSession) {
    return { kind: "proceed" };
  }

  if (credentialsState === "loading") {
    return { kind: "block-loading" };
  }

  if (credentialsState === "error") {
    return { kind: "block-retry", reason: "credentials-error" };
  }

  // credentialsState === "ready". `data` may still be undefined transiently
  // (e.g. cache eviction); treat as not yet ready and let the caller retry.
  if (!credentials) {
    return { kind: "block-loading" };
  }

  const resolution = resolveLoginBlockCredential({ block, credentials });
  if (resolution.status === "credential-not-in-list") {
    return { kind: "block-retry", reason: "credential-not-found" };
  }

  const credentialBrowserProfileId =
    resolution.status === "resolved" ? resolution.browserProfileId : null;
  const compatibility = evaluateDebugSessionProfileCompatibility({
    pbsBrowserProfileId,
    credentialBrowserProfileId,
  });
  if (!compatibility.compatible && compatibility.reason) {
    return { kind: "show-modal", reason: compatibility.reason };
  }
  return { kind: "proceed" };
}

/**
 * Translate the backend-authoritative verdict into the same gate action shape
 * the client-side fast path produces, so callers can fold the recovery branch
 * back into a single dispatch. Used when `decideDebugLoginPlayGate` returned
 * `block-retry` with reason `credential-not-found` — i.e. the bounded
 * credentials window didn't see the credential and the FE asked the backend
 * to resolve it through the org-scoped lookup the run path uses.
 */
export function gateActionFromBackendCompatibility(
  response: DebugLoginBlockCompatibilityResponse,
): DebugLoginPlayGateAction {
  if (response.compatible) {
    return { kind: "proceed" };
  }
  if (response.reason === null) {
    return { kind: "block-retry", reason: "credentials-error" };
  }
  return { kind: "show-modal", reason: response.reason };
}
