import type { GoogleOAuthCredential } from "@/api/types";
import { getGoogleOAuthCredentialScopesGranted } from "@/hooks/useGoogleOAuthCredentials";

function rowKey(credential: GoogleOAuthCredential): string {
  const scopes = [...getGoogleOAuthCredentialScopesGranted(credential)].sort();
  return [credential.state ?? "", ...scopes].join(" ");
}

// A popup that closes leaves no result of its own, so the credential rows decide: a new row, or a new state or
// scopes on the row being reconnected (Google reconnects in place and keeps the id).
export function googleConnectionChanged(
  before: GoogleOAuthCredential[],
  after: GoogleOAuthCredential[],
  reconnectConnectionId: string | null,
): boolean {
  const keysBefore = new Map(before.map((row) => [row.id, rowKey(row)]));
  return after.some(
    (row) =>
      !keysBefore.has(row.id) ||
      (row.id === reconnectConnectionId &&
        keysBefore.get(row.id) !== rowKey(row)),
  );
}
