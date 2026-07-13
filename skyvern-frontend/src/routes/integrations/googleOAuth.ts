export const GOOGLE_OAUTH_REDIRECT_PATH = "/integrations/google/callback";

const INTEGRATION_STORAGE_PREFIX = "skyvern:google-oauth-integration:";

export function buildGoogleOAuthRedirectUri(): string {
  return `${window.location.origin}${GOOGLE_OAUTH_REDIRECT_PATH}`;
}

export function getGoogleOAuthAppOrigin(): string {
  return window.location.origin;
}

export function storeGoogleOAuthIntegrationIdForState(
  state: string,
  integrationId: string,
): void {
  try {
    window.sessionStorage.setItem(
      `${INTEGRATION_STORAGE_PREFIX}${state}`,
      integrationId,
    );
  } catch {
    // Best effort; callback handling does not depend on this metadata.
  }
}

export function clearStoredGoogleOAuthIntegrationIdForState(
  state: string,
): void {
  try {
    window.sessionStorage.removeItem(`${INTEGRATION_STORAGE_PREFIX}${state}`);
  } catch {
    // Best effort cleanup.
  }
}
