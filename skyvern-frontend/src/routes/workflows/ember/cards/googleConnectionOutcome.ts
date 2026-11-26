import type { GoogleOAuthCredential } from "@/api/types";
import {
  getGoogleOAuthCredentialScopesGranted,
  hasGoogleOAuthCredentialScopes,
  isGoogleOAuthCredentialActive,
} from "@/hooks/useGoogleOAuthCredentials";

export type GoogleConnectionOutcome =
  | { kind: "connected"; connectionId: string }
  | { kind: "not_completed" }
  // A server that names no credential for the attempt cannot say whether it finished, so the picker asks.
  | { kind: "unattributed" }
  | { kind: "partial"; connectionId: string };

// The credential the authorize call said this sign-in creates or reconnects.
export type GoogleConnectionAttempt = {
  credentialId: string;
  reconnect: boolean;
};

const NOT_COMPLETED: GoogleConnectionOutcome = { kind: "not_completed" };

function rowKey(credential: GoogleOAuthCredential): string {
  const scopes = [...getGoogleOAuthCredentialScopesGranted(credential)].sort();
  return [credential.state ?? "", ...scopes].join(" ");
}

// A popup that closes leaves no result of its own, so only the attempt's own row decides: a new row is listed
// once the callback promotes it, and a reconnected row (same id) counts only when it was loaded at the click and
// has changed since. Any other row, however new, is never this attempt's result.
export function googleConnectionOutcome(
  before: GoogleOAuthCredential[],
  after: GoogleOAuthCredential[],
  attempt: GoogleConnectionAttempt | null,
  requiredScopes: readonly string[],
): GoogleConnectionOutcome {
  if (!attempt) return { kind: "unattributed" };
  const row = after.find(
    (credential) => credential.id === attempt.credentialId,
  );
  const prior = before.find(
    (credential) => credential.id === attempt.credentialId,
  );
  if (
    !row ||
    (attempt.reconnect && (!prior || rowKey(prior) === rowKey(row)))
  ) {
    return NOT_COMPLETED;
  }
  return isGoogleOAuthCredentialActive(row) &&
    hasGoogleOAuthCredentialScopes(row, requiredScopes)
    ? { kind: "connected", connectionId: row.id }
    : { kind: "partial", connectionId: row.id };
}
