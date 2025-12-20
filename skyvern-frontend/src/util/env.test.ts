import { afterEach, describe, expect, test, vi } from "vitest";

async function loadEnv(wssBaseUrl: string) {
  vi.stubEnv("VITE_WSS_BASE_URL", wssBaseUrl);
  vi.stubEnv("VITE_API_BASE_URL", "https://example.com/api/v1");
  vi.stubEnv("VITE_ENVIRONMENT", "test");
  vi.stubEnv("VITE_ARTIFACT_API_BASE_URL", "https://example.com/artifacts");
  vi.stubEnv("VITE_SKYVERN_API_KEY", "test-key");
  vi.stubEnv("VITE_API_PATH_PREFIX", "");
  vi.resetModules();
  return await import("./env");
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe("env websocket base URLs", () => {
  test("strips /api for browser session streams", async () => {
    const env = await loadEnv("wss://stream.example.com/api/v1");

    expect(env.newWssBaseUrl).toBe("wss://stream.example.com/v1");
    expect(env.legacyWssBaseUrl).toBe("wss://stream.example.com/api/v1");
  });

  test("adds /api for legacy task/workflow streams", async () => {
    const env = await loadEnv("wss://stream.example.com/v1");

    expect(env.newWssBaseUrl).toBe("wss://stream.example.com/v1");
    expect(env.legacyWssBaseUrl).toBe("wss://stream.example.com/api/v1");
  });
});
