import type { CredentialBackend } from "./types";

// The accounts the account picker has loaded, by connection id. Connection ids are never reused, so an
// entry stays true for the session; an account the picker has not loaded is simply absent.
const backends = new Map<string, CredentialBackend>();

export function rememberAccounts(
  connectionIds: readonly string[],
  backend: CredentialBackend,
) {
  for (const id of connectionIds) backends.set(id, backend);
}

export function knownAccountBackends(): ReadonlyMap<string, CredentialBackend> {
  return backends;
}
