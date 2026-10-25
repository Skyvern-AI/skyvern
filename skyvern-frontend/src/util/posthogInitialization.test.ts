import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const posthogMock = vi.hoisted(() => ({ init: vi.fn() }));

vi.mock("posthog-js", () => ({ default: posthogMock }));

async function initializePostHogFromRuntimeConfig() {
  const module = await import("./posthogInitialization");
  return module.initializePostHogFromRuntimeConfig();
}

describe("PostHog runtime initialization", () => {
  beforeEach(() => {
    vi.resetModules();
    posthogMock.init.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("does not initialize PostHog when the runtime key is unset", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ api_key: "", api_host: "" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(initializePostHogFromRuntimeConfig()).resolves.toBe(false);

    expect(posthogMock.init).not.toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      "/posthog-config",
      expect.objectContaining({
        cache: "no-store",
        headers: { accept: "application/json" },
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("initializes with the runtime key and host when configured", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({
        api_key: "configured-public-key",
        api_host: "https://posthog.example",
      }),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(initializePostHogFromRuntimeConfig()).resolves.toBe(true);

    expect(posthogMock.init).toHaveBeenCalledWith(
      "configured-public-key",
      expect.objectContaining({
        api_host: "https://posthog.example",
        before_send: expect.any(Function),
      }),
    );
  });

  it("aborts a stalled runtime config request without initializing PostHog", async () => {
    vi.useFakeTimers();
    let requestSignal: AbortSignal | undefined;
    const fetchMock = vi.fn(
      (_input: RequestInfo | URL, options?: RequestInit) =>
        new Promise<Response>((_resolve, reject) => {
          const signal = options?.signal;
          if (!signal) {
            throw new Error("Expected an abort signal");
          }
          requestSignal = signal as AbortSignal;
          requestSignal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const { initializePostHogFromRuntimeConfig } =
      await import("./posthogInitialization");
    const initialization = initializePostHogFromRuntimeConfig();
    await vi.advanceTimersByTimeAsync(3000);
    await expect(initialization).resolves.toBe(false);

    expect(requestSignal?.aborted).toBe(true);
    expect(posthogMock.init).not.toHaveBeenCalled();
  });
});
