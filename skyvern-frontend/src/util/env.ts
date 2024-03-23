const apiBaseUrl = import.meta.env.VITE_API_BASE_URL as string;

if (!apiBaseUrl) {
  console.error("apiBaseUrl environment variable was not set");
}

const environment = import.meta.env.VITE_ENVIRONMENT as string;

if (!environment) {
  console.error("environment environment variable was not set");
}

const credential = import.meta.env.VITE_API_CREDENTIAL as string;

if (!credential) {
  console.error("credential environment variable was not set");
}

export { apiBaseUrl, environment, credential };
