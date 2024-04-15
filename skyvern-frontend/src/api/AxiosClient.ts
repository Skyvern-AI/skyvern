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
  delete client.defaults.headers.common["Authorization"];
}

export function setApiKeyHeader(apiKey: string) {
  client.defaults.headers.common["X-API-Key"] = apiKey;
}

export function removeApiKeyHeader() {
  delete client.defaults.headers.common["X-API-Key"];
}

export { client, artifactApiClient };
