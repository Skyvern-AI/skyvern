export const GOOGLE_OAUTH_REDIRECT_PATH = "/integrations/google/callback";

const INTEGRATION_STORAGE_PREFIX = "skyvern:google-oauth-integration:";

const STABLE_CALLBACK_HOST_BY_API_BASE: Record<string, string> = {
  "https://api.skyvern.com/api/v1": "https://app.skyvern.com",
  "https://api-staging.skyvern.com/api/v1": "https://app-staging.skyvern.com",
  "https://api.eu.skyvern.com/api/v1": "https://app.eu.skyvern.com",
};

export function buildGoogleOAuthRedirectUri(): string {
  const apiBase = import.meta.env.VITE_API_BASE_URL ?? "";
  const callbackHost = STABLE_CALLBACK_HOST_BY_API_BASE[apiBase];
  return `${callbackHost ?? window.location.origin}${GOOGLE_OAUTH_REDIRECT_PATH}`;
}

export function getGoogleOAuthAppOrigin(): string {
  return window.location.origin;
}

export function storeGoogleOAuthIntegrationIdForState(
  state: string,
  integrationId: string,
  storageWindow: Pick<Window, "sessionStorage"> = window,
): void {
  try {
    storageWindow.sessionStorage.setItem(
      `${INTEGRATION_STORAGE_PREFIX}${state}`,
      integrationId,
    );
  } catch {
    // Best effort; callback handling does not depend on this metadata.
  }
}

export function getStoredGoogleOAuthIntegrationIdForState(
  state: string,
): string | null {
  try {
    return window.sessionStorage.getItem(
      `${INTEGRATION_STORAGE_PREFIX}${state}`,
    );
  } catch {
    return null;
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
