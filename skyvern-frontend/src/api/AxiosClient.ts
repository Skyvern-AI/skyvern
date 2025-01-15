import { apiBaseUrl, artifactApiBaseUrl, envCredential } from "@/util/env";
import axios from "axios";

const apiV1BaseUrl = apiBaseUrl;
const apiV2BaseUrl = apiBaseUrl.replace("v1", "v2");

const client = axios.create({
  baseURL: apiV1BaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-api-key": envCredential,
  },
});

const v2Client = axios.create({
  baseURL: apiV2BaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-api-key": envCredential,
  },
});

const artifactApiClient = axios.create({
  baseURL: artifactApiBaseUrl,
});

export function setAuthorizationHeader(token: string) {
  client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
  v2Client.defaults.headers.common["Authorization"] = `Bearer ${token}`;
}

export function removeAuthorizationHeader() {
  if (client.defaults.headers.common["Authorization"]) {
    delete client.defaults.headers.common["Authorization"];
    delete v2Client.defaults.headers.common["Authorization"];
  }
}

export function setApiKeyHeader(apiKey: string) {
  client.defaults.headers.common["X-API-Key"] = apiKey;
  v2Client.defaults.headers.common["X-API-Key"] = apiKey;
}

export function removeApiKeyHeader() {
  if (client.defaults.headers.common["X-API-Key"]) {
    delete client.defaults.headers.common["X-API-Key"];
  }
  if (v2Client.defaults.headers.common["X-API-Key"]) {
    delete v2Client.defaults.headers.common["X-API-Key"];
  }
}

async function getClient(
  credentialGetter: CredentialGetter | null,
  version: string = "v1",
) {
  if (credentialGetter) {
    removeApiKeyHeader();
    const credential = await credentialGetter();
    if (!credential) {
      console.warn("No credential found");
      return version === "v1" ? client : v2Client;
    }
    setAuthorizationHeader(credential);
  }
  return version === "v1" ? client : v2Client;
}

export type CredentialGetter = () => Promise<string | null>;

export { getClient, artifactApiClient };
