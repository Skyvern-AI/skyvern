const apiBaseUrl = import.meta.env.VITE_API_BASE_URL as string;

if (!apiBaseUrl) {
  console.error("apiBaseUrl environment variable was not set");
}

const environment = import.meta.env.VITE_ENVIRONMENT as string;

if (!environment) {
  console.error("environment environment variable was not set");
}

const envCredential: string | null =
  import.meta.env.VITE_API_CREDENTIAL ?? null;

const artifactApiBaseUrl = import.meta.env.VITE_ARTIFACT_API_BASE_URL;

if (!artifactApiBaseUrl) {
  console.error("artifactApiBaseUrl environment variable was not set");
}

export { apiBaseUrl, environment, envCredential, artifactApiBaseUrl };
