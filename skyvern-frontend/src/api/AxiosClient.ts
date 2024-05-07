import { apiBaseUrl, artifactApiBaseUrl, envCredential } from "@/util/env";
import axios from "axios";

const client = axios.create({
  baseURL: apiBaseUrl,
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
}

export function removeAuthorizationHeader() {
  if (client.defaults.headers.common["Authorization"]) {
    delete client.defaults.headers.common["Authorization"];
  }
}

export function setApiKeyHeader(apiKey: string) {
  client.defaults.headers.common["X-API-Key"] = apiKey;
}

export function removeApiKeyHeader() {
  if (client.defaults.headers.common["X-API-Key"]) {
    delete client.defaults.headers.common["X-API-Key"];
  }
}

async function getClient(credentialGetter: CredentialGetter | null) {
  if (credentialGetter) {
    removeApiKeyHeader();
    const credential = await credentialGetter();
    if (!credential) {
      console.warn("No credential found");
      return client;
    }
    setAuthorizationHeader(credential);
  }
  return client;
}

export type CredentialGetter = () => Promise<string | null>;

export { getClient, artifactApiClient };
