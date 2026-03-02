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

const clients = [client, v2Client, clientSansApiV1] as const;

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

export function setAuthorizationHeader(token: string) {
  setHeaderForAllClients("Authorization", `Bearer ${token}`);
}

export function removeAuthorizationHeader() {
  removeHeaderForAllClients("Authorization");
}

export function setApiKeyHeader(apiKey: string) {
  persistRuntimeApiKey(apiKey);
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
