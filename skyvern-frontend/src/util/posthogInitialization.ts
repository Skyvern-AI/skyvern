import posthog from "posthog-js";

import { dropNoiseExceptions } from "@/util/posthogNoiseFilter";

const postHogConfigTimeoutMs = 3000;
let initialization: Promise<boolean> | undefined;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function initializePostHogFromRuntimeConfig(): Promise<boolean> {
  initialization ??= (async () => {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      postHogConfigTimeoutMs,
    );

    try {
      const response = await fetch("/posthog-config", {
        cache: "no-store",
        headers: { accept: "application/json" },
        signal: controller.signal,
      });
      if (!response.ok) return false;

      const payload: unknown = await response.json();
      if (!isRecord(payload)) return false;

      const apiKey =
        typeof payload.api_key === "string" ? payload.api_key.trim() : "";
      const apiHost =
        typeof payload.api_host === "string" ? payload.api_host.trim() : "";
      if (!apiKey || !apiHost) return false;

      posthog.init(apiKey, {
        api_host: apiHost,
        before_send: dropNoiseExceptions,
      });
      return true;
    } catch {
      // Analytics configuration is optional and must not block the UI.
      return false;
    } finally {
      clearTimeout(timeout);
    }
  })();

  return initialization;
}
