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

describe("getWebSocketProtocols", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    window.sessionStorage.clear();
  });

  it("carries the runtime API key and the bearer token as subprotocols", async () => {
    const { getWebSocketProtocols, persistRuntimeApiKey } =
      await loadEnv("ignored-build-key");
    persistRuntimeApiKey("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvXzEifQ.sig-_1");

    expect(
      await getWebSocketProtocols(async () => "eyJ0eXAiOiJKV1QifQ.e30.c2ln"),
    ).toEqual([
      "skyvern.v1",
      "apikey.eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJvXzEifQ.sig-_1",
      "bearer.eyJ0eXAiOiJKV1QifQ.e30.c2ln",
    ]);
  });

  it("offers only the stream subprotocol when no credential is available", async () => {
    const { getWebSocketProtocols, hasWebSocketCredential } = await loadEnv();

    const protocols = await getWebSocketProtocols(async () => null);

    expect(protocols).toEqual(["skyvern.v1"]);
    expect(hasWebSocketCredential(protocols)).toBe(false);
  });

  it("drops a credential that is not a valid subprotocol token", async () => {
    const { getWebSocketProtocols, persistRuntimeApiKey } = await loadEnv();
    persistRuntimeApiKey("local+api/key");

    expect(await getWebSocketProtocols(async () => "clerk token")).toEqual([
      "skyvern.v1",
    ]);
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

  it("does not expose the build-time API key through stream subprotocols", async () => {
    const { getWebSocketProtocols } = await loadEnv(
      "build-time-key-must-stay-server-side",
    );

    expect(await getWebSocketProtocols(null)).toEqual(["skyvern.v1"]);
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
