import type { AxiosInstance, AxiosRequestConfig, AxiosResponse } from "axios";

const KEY_PREFIX = "copilot-credential-recovery";

export type CredentialRecoveryHistoryResponse<T> = AxiosResponse<T> & {
  hadCredentialRecoveryToken: boolean;
};

function storageKey(workflowId: string): string {
  return `${KEY_PREFIX}:${workflowId}`;
}

export function currentCredentialRecoveryToken(
  workflowId: string | undefined,
): string | null {
  if (!workflowId) return null;
  try {
    return sessionStorage.getItem(storageKey(workflowId));
  } catch {
    return null;
  }
}

export function ensureCredentialRecoveryToken(
  workflowId: string | undefined,
): string | null {
  if (!workflowId) return null;
  try {
    // Keep one capability for this workflow for the lifetime of the browser
    // tab. Each server pause still binds its digest to one org/chat/turn and
    // requires its own single-use resume token.
    const current = sessionStorage.getItem(storageKey(workflowId));
    if (current) return current;
    const token = Array.from(
      crypto.getRandomValues(new Uint8Array(32)),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    sessionStorage.setItem(storageKey(workflowId), token);
    return token;
  } catch {
    return null;
  }
}

export function credentialRecoveryHeaders(
  workflowId: string | undefined,
): Record<string, string> {
  const token = currentCredentialRecoveryToken(workflowId);
  return token ? { "X-Copilot-Credential-Recovery-Token": token } : {};
}

export class CredentialRecoveryHistoryError extends Error {
  readonly status: number | null;

  constructor(error: unknown) {
    const status =
      (error as { response?: { status?: unknown } })?.response?.status ?? null;
    const safeStatus = typeof status === "number" ? status : null;
    super(
      safeStatus === null
        ? "Copilot history request failed"
        : `Copilot history request failed (${safeStatus})`,
    );
    this.name = "CredentialRecoveryHistoryError";
    this.status = safeStatus;
  }
}

export async function readCredentialRecoveryHistory<T>(
  client: Pick<AxiosInstance, "get">,
  workflowId: string | undefined,
  config: AxiosRequestConfig,
  options: { retryTransientFailure?: boolean } = {},
): Promise<CredentialRecoveryHistoryResponse<T>> {
  const attempts = options.retryTransientFailure ? 2 : 1;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const recoveryHeaders = credentialRecoveryHeaders(workflowId);
      const response = await client.get<T>("/workflow/copilot/chat-history", {
        ...config,
        headers: {
          ...config.headers,
          ...recoveryHeaders,
        },
      });
      return {
        ...response,
        hadCredentialRecoveryToken: Boolean(
          recoveryHeaders["X-Copilot-Credential-Recovery-Token"],
        ),
      };
    } catch (error) {
      // Axios errors retain request headers. Replace them before callers log
      // the failure so the capability cannot reach the browser console.
      const safeError = new CredentialRecoveryHistoryError(error);
      const retryable = safeError.status === null || safeError.status >= 500;
      if (attempt + 1 >= attempts || !retryable || config.signal?.aborted) {
        throw safeError;
      }
    }
  }
  throw new CredentialRecoveryHistoryError(null);
}
