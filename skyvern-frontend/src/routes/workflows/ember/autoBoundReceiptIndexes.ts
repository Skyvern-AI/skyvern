// Chat items are immutable in position: a repeat of the credential already bound keeps the
// receipt on its original message, while null (no receipt here) is transparent, not a reset.
export function selectAutoBoundReceiptIndexes(
  credentialIds: ReadonlyArray<string | null>,
): ReadonlySet<number> {
  const indexes = new Set<number>();
  let previousCredentialId: string | null = null;

  credentialIds.forEach((credentialId, index) => {
    if (credentialId === null || credentialId === previousCredentialId) {
      return;
    }
    indexes.add(index);
    previousCredentialId = credentialId;
  });

  return indexes;
}
