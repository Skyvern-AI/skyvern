export function validateBitwardenLoginCredential(
  collectionId: string | null,
  itemId: string | null,
  urlParameterKey: string | null,
): string | null {
  if (!collectionId && !itemId) {
    return "Collection ID or Item ID is required";
  }
  if (collectionId && !urlParameterKey) {
    return "URL Parameter Key is required when collection ID is used";
  }
  return null;
}
