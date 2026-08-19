import {
  apiBaseUrl,
  artifactApiBaseUrl,
  getRuntimeApiKey,
  getRuntimeApiKeyExpiresAt,
  persistRuntimeApiKey,
  clearRuntimeApiKey,
} from "@/util/env";
import { useAuthIssueStore } from "@/store/AuthIssueStore";
import axios, { type InternalAxiosRequestConfig } from "axios";

type ApiVersion = "sans-api-v1" | "v1" | "v2";

const apiV1BaseUrl = apiBaseUrl;
const apiV2BaseUrl = apiBaseUrl.replace("v1", "v2");
const url = new URL(apiBaseUrl);
const pathname = url.pathname.replace("/api", "");
const apiSansApiV1BaseUrl = `${url.origin}${pathname}`;

const initialApiKey = getRuntimeApiKey();

export const POSTHOG_ATTRIBUTION_HEADER = "X-PostHog-Attribution";
const apiKeyHeader = initialApiKey ? { "X-API-Key": initialApiKey } : {};

const client = axios.create({
  baseURL: apiV1BaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-user-agent": "skyvern-ui",
    ...apiKeyHeader,
  },
});

const v2Client = axios.create({
  baseURL: apiV2BaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-user-agent": "skyvern-ui",
    ...apiKeyHeader,
  },
});

const clientSansApiV1 = axios.create({
  baseURL: apiSansApiV1BaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-user-agent": "skyvern-ui",
    ...apiKeyHeader,
  },
});

const artifactApiClient = axios.create({
  baseURL: artifactApiBaseUrl,
});

const clients = [client, v2Client, clientSansApiV1] as const;

type UISessionResponse = {
  token: string;
  expires_at: number;
};

type UISessionRetryConfig = InternalAxiosRequestConfig & {
  uiSessionRetry?: boolean;
};

const UI_SESSION_REQUEST_TIMEOUT_MS = 10_000;

let uiSessionEnabled = false;
let uiSessionEndpointConfirmed = false;
// "endpoint absent" is inferred from a 404 or a non-JSON 200, and an SPA catch-all or a proxy
// mid-rollout produces exactly that for an endpoint which does exist. Require a run of them before
// concluding the deployment has no mint endpoint, so one blip cannot disable refresh for the tab.
let uiSessionEndpointAbsentStreak = 0;
let uiSessionLatchReprobed = false;
const UI_SESSION_ENDPOINT_ABSENT_LIMIT = 3;
let uiSessionInitialization: Promise<void> | null = null;
let uiSessionRefresh: Promise<boolean> | null = null;
let uiSessionRefreshBypassesCache = false;
let uiSessionRefreshTimer: ReturnType<typeof setTimeout> | null = null;

function isUiSessionResponse(value: unknown): value is UISessionResponse {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<UISessionResponse>;
  return (
    typeof candidate.token === "string" &&
    candidate.token.length > 0 &&
    typeof candidate.expires_at === "number" &&
    Number.isFinite(candidate.expires_at) &&
    candidate.expires_at * 1000 > Date.now()
  );
}

function scheduleUiSessionRefresh(expiresAt: number) {
  if (uiSessionRefreshTimer) {
    clearTimeout(uiSessionRefreshTimer);
  }
  const remainingLifetime = expiresAt * 1000 - Date.now();
  uiSessionRefreshTimer = setTimeout(
    () => {
      void refreshUiSession();
    },
    Math.max(0, remainingLifetime / 2),
  );
}

async function requestUiSession(bypassCache = false): Promise<boolean> {
  const abortController = new AbortController();
  const timeoutId = setTimeout(
    () => abortController.abort(),
    UI_SESSION_REQUEST_TIMEOUT_MS,
  );
  try {
    const response = await fetch("/ui-session", {
      cache: "no-store",
      headers: {
        accept: "application/json",
        ...(bypassCache
          ? { "x-skyvern-ui-session-refresh": "auth-failure" }
          : {}),
      },
      signal: abortController.signal,
    });
    const contentType = response.headers.get("content-type") ?? "";
    const hasJsonContentType = contentType.includes("application/json");
    const endpointAbsent =
      response.status === 404 || (response.ok && !hasJsonContentType);
    if (!response.ok || !hasJsonContentType) {
      if (!uiSessionEndpointConfirmed && endpointAbsent) {
        uiSessionEndpointAbsentStreak += 1;
        if (uiSessionEndpointAbsentStreak >= UI_SESSION_ENDPOINT_ABSENT_LIMIT) {
          uiSessionEnabled = false;
        }
      }
      return false;
    }
    const payload: unknown = await response.json();
    if (!isUiSessionResponse(payload)) {
      return false;
    }
    uiSessionEndpointConfirmed = true;
    uiSessionEndpointAbsentStreak = 0;
    setApiKeyHeader(payload.token, payload.expires_at);
    scheduleUiSessionRefresh(payload.expires_at);
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timeoutId);
  }
}

async function refreshUiSession(bypassCache = false): Promise<boolean> {
  if (!uiSessionEnabled) {
    // A 401/403 is evidence the deployment does want a credential, so a latch set by ambiguous
    // absent-endpoint responses may have been wrong. Probe exactly once per page load, and only
    // lift the latch if that probe actually mints — re-enabling first would let a genuinely absent
    // endpoint be polled again on every later failure.
    if (!bypassCache || uiSessionLatchReprobed) {
      return false;
    }
    uiSessionLatchReprobed = true;
    const recovered = await requestUiSession(true);
    if (recovered) {
      uiSessionEnabled = true;
      uiSessionEndpointAbsentStreak = 0;
    }
    return recovered;
  }
  if (uiSessionRefresh) {
    if (bypassCache && !uiSessionRefreshBypassesCache) {
      await uiSessionRefresh;
      return await refreshUiSession(true);
    }
    return await uiSessionRefresh;
  }
  uiSessionRefreshBypassesCache = bypassCache;
  uiSessionRefresh = requestUiSession(bypassCache).finally(() => {
    uiSessionRefresh = null;
    uiSessionRefreshBypassesCache = false;
  });
  return await uiSessionRefresh;
}

export function initializeUiSession(): Promise<void> {
  if (!uiSessionInitialization) {
    uiSessionEnabled = true;
    uiSessionInitialization = (async () => {
      const apiKey = getRuntimeApiKey();
      const expiresAt = getRuntimeApiKeyExpiresAt();
      if (apiKey && expiresAt && expiresAt * 1000 > Date.now()) {
        uiSessionEndpointConfirmed = true;
        scheduleUiSessionRefresh(expiresAt);
      } else {
        const initialized = await refreshUiSession();
        if (!initialized && uiSessionEnabled) {
          uiSessionInitialization = null;
        }
      }
    })();
  }
  return uiSessionInitialization;
}

function getResponseDetail(data: unknown): string | undefined {
  if (data && typeof data === "object" && "detail" in data) {
    const detail = (data as { detail?: unknown }).detail;
    return typeof detail === "string" ? detail : undefined;
  }
  return undefined;
}

clients.forEach((instance) => {
  instance.interceptors.response.use(
    (response) => response,
    async (error) => {
      if (axios.isAxiosError(error)) {
        const statusCode = error.response?.status;
        const detail = getResponseDetail(error.response?.data);
        const retryConfig = error.config as UISessionRetryConfig | undefined;
        if (
          (statusCode === 401 || statusCode === 403) &&
          retryConfig &&
          !retryConfig.uiSessionRetry
        ) {
          retryConfig.uiSessionRetry = true;
          if (await refreshUiSession(true)) {
            const apiKey = getRuntimeApiKey();
            if (apiKey) {
              retryConfig.headers.set("X-API-Key", apiKey);
              return await instance.request(retryConfig);
            }
          }
        }
        const isAuthFailure =
          statusCode === 401 ||
          statusCode === 403 ||
          (statusCode === 404 && detail === "Organization not found");

        if (statusCode && isAuthFailure) {
          useAuthIssueStore.getState().reportAuthIssue({
            statusCode,
            detail,
            path: error.config?.url,
          });
        }
      }
      return Promise.reject(error);
    },
  );
});

function setHeaderForAllClients(header: string, value: string) {
  clients.forEach((instance) => {
    instance.defaults.headers.common[header] = value;
  });
}

function removeHeaderForAllClients(header: string) {
  clients.forEach((instance) => {
    // Axios stores headers at both `common` (shared across methods) and the
    // top-level defaults object (can be set by initial config spread). Delete
    // from both locations to ensure the header is fully removed.
    delete instance.defaults.headers.common[header];
    delete (instance.defaults.headers as Record<string, unknown>)[header];
  });
}

export function setPostHogAttributionHeader(value: string | undefined) {
  if (value) {
    setHeaderForAllClients(POSTHOG_ATTRIBUTION_HEADER, value);
  } else {
    removeHeaderForAllClients(POSTHOG_ATTRIBUTION_HEADER);
  }
}

export function setAuthorizationHeader(token: string) {
  setHeaderForAllClients("Authorization", `Bearer ${token}`);
}

export function removeAuthorizationHeader() {
  removeHeaderForAllClients("Authorization");
}

export function setApiKeyHeader(apiKey: string, expiresAt?: number) {
  persistRuntimeApiKey(apiKey, expiresAt);
  setHeaderForAllClients("X-API-Key", apiKey);
}

export function removeApiKeyHeader({
  clearStoredKey = true,
}: {
  clearStoredKey?: boolean;
} = {}) {
  if (clearStoredKey) {
    clearRuntimeApiKey();
  }
  removeHeaderForAllClients("X-API-Key");
}

async function getClient(
  credentialGetter: CredentialGetter | null,
  version: ApiVersion = "v1",
) {
  const get = () => {
    switch (version) {
      case "sans-api-v1":
        return clientSansApiV1;
      case "v1":
        return client;
      case "v2":
        return v2Client;
      default: {
        throw new Error(`Unknown version: ${version}`);
      }
    }
  };

  // Both auth headers are sent intentionally: Authorization (Bearer token from
  // the credential getter, e.g. Clerk) is used for user-session auth, while
  // X-API-Key is used for org-level API key auth. The backend accepts either
  // and gives precedence to the API key when both are present. Sending both
  // ensures requests succeed regardless of which auth method the org uses.
  const credential = credentialGetter ? await credentialGetter() : null;
  if (credential) {
    setAuthorizationHeader(credential);
  } else {
    removeAuthorizationHeader();
  }

  const apiKey = getRuntimeApiKey();
  if (apiKey) {
    setHeaderForAllClients("X-API-Key", apiKey);
  } else {
    // Avoid mutating persisted keys here - just control request headers.
    removeApiKeyHeader({ clearStoredKey: false });
  }

  return get();
}

export type CredentialGetter = () => Promise<string | null>;

export { getClient, artifactApiClient };
