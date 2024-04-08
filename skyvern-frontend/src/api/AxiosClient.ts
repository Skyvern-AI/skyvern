import { apiBaseUrl, artifactApiBaseUrl, credential } from "@/util/env";
import axios from "axios";

const client = axios.create({
  baseURL: apiBaseUrl,
  headers: {
    "Content-Type": "application/json",
    "x-api-key": credential,
  },
});

const artifactApiClient = axios.create({
  baseURL: artifactApiBaseUrl,
});

export { client, artifactApiClient };
