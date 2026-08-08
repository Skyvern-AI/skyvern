import { afterEach, describe, expect, it, vi } from "vitest";

async function loadEnv(apiKey: string | null = null, streamingMode = "") {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8000/api/v1");
  vi.stubEnv("VITE_ARTIFACT_API_BASE_URL", "http://localhost:9090");
  vi.stubEnv("VITE_ENVIRONMENT", "test");
  vi.stubEnv("VITE_WSS_BASE_URL", "ws://localhost:8000/api/v1");
  vi.stubEnv("VITE_BROWSER_STREAMING_MODE", streamingMode);
  if (apiKey) {
    vi.stubEnv("VITE_SKYVERN_API_KEY", apiKey);
  } else {
    vi.stubEnv("VITE_SKYVERN_API_KEY", "");
  }
  return import("./env");
}

describe("getCredentialParam", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    window.sessionStorage.clear();
  });

  it("includes the runtime API key even when a token getter returns a token", async () => {
    const { getCredentialParam, persistRuntimeApiKey } =
      await loadEnv("ignored-build-key");
    persistRuntimeApiKey("local+api/key");

    const params = new URLSearchParams(
      await getCredentialParam(async () => "clerk token"),
    );

    expect(params.get("apikey")).toBe("local+api/key");
    expect(params.get("token")).toBe("Bearer clerk token");
  });

  it("uses the token when no runtime API key is available", async () => {
    const { getCredentialParam } = await loadEnv();

    const params = new URLSearchParams(
      await getCredentialParam(async () => "clerk token"),
    );

    expect(params.has("apikey")).toBe(false);
    expect(params.get("token")).toBe("Bearer clerk token");
  });
});

describe("getRuntimeApiKey", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    window.sessionStorage.clear();
  });

  it("ignores the build-time API key", async () => {
    const { getRuntimeApiKey } = await loadEnv("ignored-build-key-canary");

    expect(getRuntimeApiKey()).toBe(null);
    expect(window.sessionStorage.getItem("skyvern.apiKey")).toBe(null);
  });

  it("does not expose the server-side API key to the browser", async () => {
    vi.stubEnv("SKYVERN_API_KEY", "server-only-key-canary");
    const { getRuntimeApiKey } = await loadEnv();

    expect(getRuntimeApiKey()).toBe(null);
    expect(window.sessionStorage.getItem("skyvern.apiKey")).toBe(null);
  });

  it("does not expose the build-time API key through credential params", async () => {
    const { getCredentialParam } = await loadEnv(
      "build-time-key-must-stay-server-side",
    );

    const params = new URLSearchParams(await getCredentialParam(null));

    expect(params.has("apikey")).toBe(false);
  });

  it("persists and clears a runtime API key and expiry in sessionStorage", async () => {
    const {
      clearRuntimeApiKey,
      getRuntimeApiKey,
      getRuntimeApiKeyExpiresAt,
      persistRuntimeApiKey,
    } = await loadEnv();

    persistRuntimeApiKey("runtime-key-canary", 1_893_456_789);
    expect(getRuntimeApiKey()).toBe("runtime-key-canary");
    expect(getRuntimeApiKeyExpiresAt()).toBe(1_893_456_789);
    expect(window.sessionStorage.getItem("skyvern.apiKey")).toBe(
      "runtime-key-canary",
    );
    expect(window.sessionStorage.getItem("skyvern.apiKeyExpiresAt")).toBe(
      "1893456789",
    );

    clearRuntimeApiKey();
    expect(getRuntimeApiKey()).toBe(null);
    expect(getRuntimeApiKeyExpiresAt()).toBe(null);
    expect(window.sessionStorage.getItem("skyvern.apiKey")).toBe(null);
    expect(window.sessionStorage.getItem("skyvern.apiKeyExpiresAt")).toBe(null);
  });

  it("clears a persisted expiry when replacing a session token without one", async () => {
    const { getRuntimeApiKeyExpiresAt, persistRuntimeApiKey } = await loadEnv();

    persistRuntimeApiKey("ui-session-canary", 1_893_456_789);
    persistRuntimeApiKey("api-key-canary");

    expect(getRuntimeApiKeyExpiresAt()).toBe(null);
    expect(window.sessionStorage.getItem("skyvern.apiKeyExpiresAt")).toBe(null);
  });
});

describe("browserStreamingMode", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("preserves VNC behavior when no streaming mode is configured", async () => {
    const { browserStreamingMode } = await loadEnv();

    expect(browserStreamingMode).toBe("vnc");
  });

  it("uses the configured streaming mode when present", async () => {
    const { browserStreamingMode } = await loadEnv(null, "CDP");

    expect(browserStreamingMode).toBe("cdp");
  });
});
