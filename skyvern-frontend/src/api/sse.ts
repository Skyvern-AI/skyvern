import { fetchEventSource } from "@microsoft/fetch-event-source";
import type { CredentialGetter } from "@/api/AxiosClient";
import { getRuntimeApiKey, runsApiBaseUrl } from "@/util/env";

export type SseJsonPayload = Record<string, unknown>;

type SseClient = {
  post: <T extends SseJsonPayload>(path: string, body: unknown) => Promise<T>;
};

export async function fetchJsonSse<T extends SseJsonPayload>(
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<T> {
  const controller = new AbortController();
  try {
    const parsedPayload = await new Promise<T>((resolve, reject) => {
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
            resolve(payload);
          } catch (error) {
            reject(error);
          }
        },
        onerror: (error) => {
          reject(error);
        },
        onopen: async (response) => {
          if (!response.ok) {
            const errorText = await response.text();
            reject(new Error(errorText || "Failed to send request."));
          }
        },
      }).catch(reject);
    });

    return parsedPayload;
  } finally {
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
  } else {
    const apiKey = getRuntimeApiKey();
    if (apiKey) {
      requestHeaders["X-API-Key"] = apiKey;
    }
  }

  return {
    post: <T extends SseJsonPayload>(path: string, body: unknown) => {
      return fetchJsonSse<T>(
        `${runsApiBaseUrl.replace(/\/$/, "")}/${path.replace(/^\//, "")}`,
        {
          method: "POST",
          headers: requestHeaders,
          body: JSON.stringify(body),
        },
      );
    },
  };
}
