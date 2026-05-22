import { afterEach, describe, expect, it, vi } from "vitest";

async function loadRuntimeConfigHelpers() {
  vi.resetModules();
  vi.stubEnv("VITE_API_BASE_URL", "http://localhost:8000/api/v1");
  vi.stubEnv("VITE_ARTIFACT_API_BASE_URL", "http://localhost:9090");
  vi.stubEnv("VITE_ENVIRONMENT", "test");
  vi.stubEnv("VITE_WSS_BASE_URL", "ws://localhost:8000/api/v1");
  vi.stubEnv("VITE_BROWSER_STREAMING_MODE", "cdp");
  return import("./useRuntimeConfig");
}

describe("runtime config helpers", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  it("normalizes supported browser streaming modes", async () => {
    const { normalizeBrowserStreamingMode } = await loadRuntimeConfigHelpers();

    expect(normalizeBrowserStreamingMode("CDP")).toBe("cdp");
    expect(normalizeBrowserStreamingMode("vnc")).toBe("vnc");
  });

  it("falls back to vnc for invalid browser streaming modes", async () => {
    const { normalizeBrowserStreamingMode } = await loadRuntimeConfigHelpers();

    expect(normalizeBrowserStreamingMode("unexpected")).toBe("vnc");
    expect(normalizeBrowserStreamingMode(undefined)).toBe("vnc");
  });
});
