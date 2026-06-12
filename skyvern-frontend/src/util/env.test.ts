import { readFile } from "node:fs/promises";
import path from "node:path";

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

describe("getRuntimeApiKey", () => {
  // Built via join so this test file never contains the sentinel verbatim
  // either — entrypoint-skyvernui.sh sed-replaces every occurrence in built
  // assets, and the source guard below must stay meaningful.
  const dockerSentinel = ["__SKYVERN_API_KEY", "_PLACEHOLDER__"].join("");

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
    window.sessionStorage.clear();
  });

  it("returns the build-time API key", async () => {
    const { getRuntimeApiKey } = await loadEnv("eyJhbGciOiJIUzI1NiJ9.real.key");

    expect(getRuntimeApiKey()).toBe("eyJhbGciOiJIUzI1NiJ9.real.key");
  });

  it("treats the .env.example placeholder as missing", async () => {
    const { getRuntimeApiKey } = await loadEnv("YOUR_API_KEY");

    expect(getRuntimeApiKey()).toBe(null);
  });

  it("treats an un-replaced docker placeholder as missing", async () => {
    const { getRuntimeApiKey } = await loadEnv(dockerSentinel);

    expect(getRuntimeApiKey()).toBe(null);
  });

  // Regression guard for the v1.0.34–v1.0.40 docker auth outage:
  // entrypoint-skyvernui.sh sed-replaces EVERY occurrence of the docker
  // sentinel in the built bundle with the real API key. When the full
  // sentinel appeared verbatim in this module (inside the placeholder
  // deny-list), the real key replaced it there too, so the key became a
  // member of its own deny-list and the UI sent no x-api-key header at all
  // (403 "Invalid credentials" on every request).
  it("never spells out the full docker placeholder sentinel in module source", async () => {
    const source = await readFile(
      path.join(process.cwd(), "src/util/env.ts"),
      "utf-8",
    );

    expect(source.includes(dockerSentinel)).toBe(false);
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
