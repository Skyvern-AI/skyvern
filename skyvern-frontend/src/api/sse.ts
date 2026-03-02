import { fetchEventSource } from "@microsoft/fetch-event-source";
import type { CredentialGetter } from "@/api/AxiosClient";
import { getRuntimeApiKey, runsApiBaseUrl } from "@/util/env";

export type SseMessageHandler<T> = (payload: T, eventName: string) => boolean;

type SseStreamingOptions = {
  signal?: AbortSignal;
};

type SseClient = {
  postStreaming: <T>(
    path: string,
    body: unknown,
    onMessage: SseMessageHandler<T>,
    options?: SseStreamingOptions,
  ) => Promise<void>;
};

export async function fetchStreamingSse<T>(
  input: RequestInfo | URL,
  init: RequestInit,
  onMessage: SseMessageHandler<T>,
  options?: SseStreamingOptions,
): Promise<void> {
  const controller = new AbortController();
  const externalSignal = options?.signal;
  let settled = false;
  const resolveOnce = () => {
    if (!settled) {
      settled = true;
      return true;
    }
    return false;
  };
  const onExternalAbort = () => {
    controller.abort();
  };
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
      return;
    }
    externalSignal.addEventListener("abort", onExternalAbort, { once: true });
  }
  try {
    await new Promise<void>((resolve, reject) => {
      const safeResolve = () => {
        if (resolveOnce()) {
          resolve();
        }
      };
      const safeReject = (error: unknown) => {
        if (controller.signal.aborted) {
          safeResolve();
          return;
        }
        if (!settled) {
          settled = true;
          reject(error);
        }
      };

      fetchEventSource(input instanceof URL ? input.toString() : input, {
        method: init.method,
        headers: init.headers as Record<string, string>,
        body: init.body,
        signal: controller.signal,
        onmessage: (event) => {
          if (!event.data || !event.data.trim()) {
            return;
          }
          try {
            const payload = JSON.parse(event.data) as T;
            if (onMessage(payload, event.event)) {
              safeResolve();
            }
          } catch (error) {
            safeReject(error);
          }
        },
        onerror: (error) => {
          safeReject(error);
        },
        onopen: async (response) => {
          if (!response.ok) {
            const errorText = await response.text();
            safeReject(new Error(errorText || "Failed to send request."));
          }
        },
      }).catch(safeReject);
    });
  } finally {
    if (externalSignal) {
      externalSignal.removeEventListener("abort", onExternalAbort);
    }
    controller.abort();
  }
}

export async function getSseClient(
  credentialGetter: CredentialGetter | null,
): Promise<SseClient> {
  const requestHeaders: Record<string, string> = {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    "x-user-agent": "skyvern-ui",
  };

  let authToken: string | null = null;
  if (credentialGetter) {
    authToken = await credentialGetter();
  }

  if (authToken) {
    requestHeaders.Authorization = `Bearer ${authToken}`;
  }

  const apiKey = getRuntimeApiKey();
  if (apiKey) {
    requestHeaders["X-API-Key"] = apiKey;
  }

  return {
    postStreaming: <T>(
      path: string,
      body: unknown,
      onMessage: SseMessageHandler<T>,
      options?: SseStreamingOptions,
    ) => {
      return fetchStreamingSse<T>(
        `${runsApiBaseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}`,
        {
          method: "POST",
          headers: requestHeaders,
          body: JSON.stringify(body),
        },
        onMessage,
        options,
      );
    },
  };
}
