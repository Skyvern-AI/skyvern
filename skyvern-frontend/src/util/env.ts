const apiBaseUrl = import.meta.env.VITE_API_BASE_URL as string;

if (!apiBaseUrl) {
  console.warn("apiBaseUrl environment variable was not set");
}

const environment = import.meta.env.VITE_ENVIRONMENT as string;

if (!environment) {
  console.warn("environment environment variable was not set");
}

const browserStreamingMode = (
  (import.meta.env.VITE_BROWSER_STREAMING_MODE as string | undefined) || "vnc"
).toLowerCase();

const artifactApiBaseUrl = import.meta.env.VITE_ARTIFACT_API_BASE_URL;

if (!artifactApiBaseUrl) {
  console.warn("artifactApiBaseUrl environment variable was not set");
}

const apiPathPrefix = import.meta.env.VITE_API_PATH_PREFIX ?? "";

const API_KEY_STORAGE_KEY = "skyvern.apiKey";
const API_KEY_EXPIRES_AT_STORAGE_KEY = "skyvern.apiKeyExpiresAt";

const lsKeys = {
  browserSessionId: "skyvern.browserSessionId",
  apiKey: API_KEY_STORAGE_KEY,
  apiKeyExpiresAt: API_KEY_EXPIRES_AT_STORAGE_KEY,
};

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

let newWssBaseUrl = wssBaseUrl;
try {
  const url = new URL(wssBaseUrl);
  if (url.pathname.startsWith("/api")) {
    url.pathname = url.pathname.replace(/^\/api/, "");
  }
  newWssBaseUrl = url.toString();
} catch (e) {
  newWssBaseUrl = wssBaseUrl?.replace("/api", "") ?? "";
}

// Base URL for the Runs API (strip a leading `/api` segment: /api/v1 -> /v1)
const runsApiBaseUrl = (() => {
  try {
    const url = new URL(apiBaseUrl);
    if (url.pathname.startsWith("/api")) {
      url.pathname = url.pathname.replace(/^\/api/, "");
    }
    return `${url.origin}${url.pathname}`;
  } catch (e) {
    return apiBaseUrl?.replace("/api", "") ?? "";
  }
})();

let runtimeApiKey: string | null | undefined;

// Self-hosted mints the browser credential after boot, so callers that authenticate a request
// must wait for the first mint to land instead of sending it unauthenticated.
let runtimeCredentialReady: Promise<unknown> = Promise.resolve();
const runtimeCredentialListeners = new Set<() => void>();

function setRuntimeCredentialReady(ready: Promise<unknown>): void {
  runtimeCredentialReady = ready;
}

function whenRuntimeCredentialReady(): Promise<unknown> {
  return runtimeCredentialReady;
}

function subscribeToRuntimeCredential(listener: () => void): () => void {
  runtimeCredentialListeners.add(listener);
  return () => {
    runtimeCredentialListeners.delete(listener);
  };
}

function notifyRuntimeCredentialChanged(): void {
  for (const listener of runtimeCredentialListeners) {
    listener();
  }
}

function readPersistedApiKey(): string | null {
  if (typeof window === "undefined") {
    return null;
  }

  return window.sessionStorage.getItem(API_KEY_STORAGE_KEY);
}

function getRuntimeApiKey(): string | null {
  if (runtimeApiKey !== undefined) {
    return runtimeApiKey;
  }

  runtimeApiKey = readPersistedApiKey();
  return runtimeApiKey;
}

function getRuntimeApiKeyExpiresAt(): number | null {
  if (typeof window === "undefined") {
    return null;
  }

  const expiresAt = Number(
    window.sessionStorage.getItem(API_KEY_EXPIRES_AT_STORAGE_KEY),
  );
  return Number.isFinite(expiresAt) && expiresAt > 0 ? expiresAt : null;
}

function persistRuntimeApiKey(value: string, expiresAt?: number): void {
  runtimeApiKey = value;
  if (typeof window !== "undefined") {
    window.sessionStorage.setItem(API_KEY_STORAGE_KEY, value);
    if (expiresAt !== undefined && Number.isFinite(expiresAt)) {
      window.sessionStorage.setItem(
        API_KEY_EXPIRES_AT_STORAGE_KEY,
        String(expiresAt),
      );
    } else {
      window.sessionStorage.removeItem(API_KEY_EXPIRES_AT_STORAGE_KEY);
    }
  }
  notifyRuntimeCredentialChanged();
}

function clearRuntimeApiKey(): void {
  runtimeApiKey = null;
  if (typeof window !== "undefined") {
    window.sessionStorage.removeItem(API_KEY_STORAGE_KEY);
    window.sessionStorage.removeItem(API_KEY_EXPIRES_AT_STORAGE_KEY);
  }
  notifyRuntimeCredentialChanged();
}

async function getCredentialParam(
  credentialGetter: (() => Promise<string | null>) | null,
): Promise<string> {
  if (!credentialGetter) {
    await whenRuntimeCredentialReady();
  }
  const params = new URLSearchParams();
  const apiKey = getRuntimeApiKey();
  if (apiKey) {
    params.set("apikey", apiKey);
  }

  if (credentialGetter) {
    const token = await credentialGetter();
    if (token) {
      params.set("token", `Bearer ${token}`);
    }
  }

  return params.toString();
}

const useNewRunsUrl = true as const;

const enable2faNotifications =
  import.meta.env.VITE_ENABLE_2FA_NOTIFICATIONS?.toLowerCase() === "true";

export {
  apiBaseUrl,
  runsApiBaseUrl,
  environment,
  browserStreamingMode,
  artifactApiBaseUrl,
  apiPathPrefix,
  lsKeys,
  wssBaseUrl,
  newWssBaseUrl,
  getCredentialParam,
  getRuntimeApiKey,
  getRuntimeApiKeyExpiresAt,
  persistRuntimeApiKey,
  clearRuntimeApiKey,
  setRuntimeCredentialReady,
  whenRuntimeCredentialReady,
  subscribeToRuntimeCredential,
  useNewRunsUrl,
  enable2faNotifications,
};
