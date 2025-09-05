const apiBaseUrl = import.meta.env.VITE_API_BASE_URL as string;

if (!apiBaseUrl) {
  console.warn("apiBaseUrl environment variable was not set");
}

const environment = import.meta.env.VITE_ENVIRONMENT as string;

if (!environment) {
  console.warn("environment environment variable was not set");
}

const envCredential: string | null =
  import.meta.env.VITE_SKYVERN_API_KEY ?? null;

const artifactApiBaseUrl = import.meta.env.VITE_ARTIFACT_API_BASE_URL;

if (!artifactApiBaseUrl) {
  console.warn("artifactApiBaseUrl environment variable was not set");
}

const apiPathPrefix = import.meta.env.VITE_API_PATH_PREFIX ?? "";

const lsKeys = {
  browserSessionId: "skyvern.browserSessionId",
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
  newWssBaseUrl = wssBaseUrl.replace("/api", "");
}

export {
  apiBaseUrl,
  environment,
  envCredential,
  artifactApiBaseUrl,
  apiPathPrefix,
  lsKeys,
  wssBaseUrl,
  newWssBaseUrl,
};
