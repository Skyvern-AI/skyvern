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
  });

  it("includes the runtime API key even when a token getter returns a token", async () => {
    const { getCredentialParam } = await loadEnv("local+api/key");

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
