import {
  apiBaseUrl,
  artifactApiBaseUrl,
  getRuntimeApiKey,
  persistRuntimeApiKey,
  clearRuntimeApiKey,
} from "@/util/env";
import axios from "axios";

type ApiVersion = "sans-api-v1" | "v1" | "v2";

const apiV1BaseUrl = apiBaseUrl;
const apiV2BaseUrl = apiBaseUrl.replace("v1", "v2");
const url = new URL(apiBaseUrl);
const pathname = url.pathname.replace("/api", "");
const apiSansApiV1BaseUrl = `${url.origin}${pathname}`;

const initialApiKey = getRuntimeApiKey();
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

export function setAuthorizationHeader(token: string) {
  client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  v2Client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  clientSansApiV1.defaults.headers.common["Authorization"] = `Bearer ${token}`;
}

export function removeAuthorizationHeader() {
  if (client.defaults.headers.common["Authorization"]) {
    delete client.defaults.headers.common["Authorization"];
    delete v2Client.defaults.headers.common["Authorization"];
    delete clientSansApiV1.defaults.headers.common["Authorization"];
  }
}

export function setApiKeyHeader(apiKey: string) {
  persistRuntimeApiKey(apiKey);
  client.defaults.headers.common["X-API-Key"] = apiKey;
  v2Client.defaults.headers.common["X-API-Key"] = apiKey;
  clientSansApiV1.defaults.headers.common["X-API-Key"] = apiKey;
}

export function removeApiKeyHeader() {
  clearRuntimeApiKey();
  if (client.defaults.headers.common["X-API-Key"]) {
    delete client.defaults.headers.common["X-API-Key"];
  }
  if (v2Client.defaults.headers.common["X-API-Key"]) {
    delete v2Client.defaults.headers.common["X-API-Key"];
  }
  if (clientSansApiV1.defaults.headers.common["X-API-Key"]) {
    delete clientSansApiV1.defaults.headers.common["X-API-Key"];
  }
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

  if (credentialGetter) {
    removeApiKeyHeader();

    const credential = await credentialGetter();

    if (!credential) {
      console.warn("No credential found");
      return get();
    }

    setAuthorizationHeader(credential);
  }

  return get();
}

export type CredentialGetter = () => Promise<string | null>;

// Interceptor to handle 403/404 responses from internal auth status endpoint
// This prevents misleading "API key verification failed" errors when running
// self-hosted Skyvern instances accessed remotely (non-localhost)
// See: https://github.com/Skyvern-AI/skyvern/issues/3782
[client, v2Client, clientSansApiV1].forEach((instance) => {
  instance.interceptors.response.use(
    (response) => response,
    (error) => {
      try {
        const url = String(error?.config?.url || "");
        const status = Number(error?.response?.status || 0);
        // Extract 'detail' for logging purposes to aid debugging and monitoring.
        const detail = String(error?.response?.data?.detail || "");

        // Only intercept the internal auth endpoint
        const isInternalAuth =
          url.endsWith("/internal/auth/status") ||
          url.endsWith("/api/v1/internal/auth/status");

        if (isInternalAuth && (status === 403 || status === 404)) {
          console.warn(
            "Ignoring /internal/auth/status API error:",
            status,
            detail,
          );
          return Promise.resolve({
            ...error.response,
            status: 200,
            data: { status: "ok" },
          });
        }
      } catch (err) {
        console.error("Interceptor error:", err);
      }
      return Promise.reject(error);
    },
  );
});

export { getClient, artifactApiClient };
