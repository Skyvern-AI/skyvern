import type { AxiosInstance } from "axios";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  CredentialRecoveryHistoryError,
  credentialRecoveryHeaders,
  currentCredentialRecoveryToken,
  readCredentialRecoveryHistory,
  ensureCredentialRecoveryToken,
} from "./credentialRecovery";

describe("credentialRecovery", () => {
  beforeEach(() => sessionStorage.clear());

  it("A46 reports unavailable storage when reads work but writes exceed quota", () => {
    const write = vi
      .spyOn(Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("Full", "QuotaExceededError");
      });
    try {
      expect(ensureCredentialRecoveryToken("wpid-full")).toBeNull();
    } finally {
      write.mockRestore();
    }
  });

  it("retains one capability for every recoverable turn in the workflow", () => {
    const first = ensureCredentialRecoveryToken("wpid-1");
    expect(first).toMatch(/^[0-9a-f]{64}$/);
    expect(currentCredentialRecoveryToken("wpid-1")).toBe(first);
    expect(credentialRecoveryHeaders("wpid-1")).toEqual({
      "X-Copilot-Credential-Recovery-Token": first,
    });

    const second = ensureCredentialRecoveryToken("wpid-1");
    expect(second).toMatch(/^[0-9a-f]{64}$/);
    expect(second).toBe(first);
    expect(currentCredentialRecoveryToken("wpid-1")).toBe(second);
  });

  it("keeps capabilities scoped by workflow", () => {
    ensureCredentialRecoveryToken("wpid-1");
    expect(currentCredentialRecoveryToken("wpid-2")).toBeNull();
    expect(credentialRecoveryHeaders(undefined)).toEqual({});
  });

  it("does not expose the capability when a history read fails", async () => {
    const token = ensureCredentialRecoveryToken("wpid-1");
    const client = {
      get: async () => {
        throw {
          response: { status: 403 },
          config: { headers: { "X-Copilot-Credential-Recovery-Token": token } },
        };
      },
    };

    const error = await readCredentialRecoveryHistory(
      client,
      "wpid-1",
      {},
    ).catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(CredentialRecoveryHistoryError);
    expect(error).toMatchObject({ status: 403 });
    expect(String(error)).not.toContain(token);
    expect(error).not.toHaveProperty("config");
  });

  it("retries one transient authenticated history failure", async () => {
    ensureCredentialRecoveryToken("wpid-1");
    let reads = 0;
    const get = vi.fn(async () => {
      reads += 1;
      if (reads === 1) throw new Error("temporary network failure");
      return { data: { workflow_copilot_chat_id: "chat-1" } };
    });
    const client = { get } as unknown as Pick<AxiosInstance, "get">;

    const response = await readCredentialRecoveryHistory<{
      workflow_copilot_chat_id: string;
    }>(client, "wpid-1", {}, { retryTransientFailure: true });

    expect(reads).toBe(2);
    expect(response.data.workflow_copilot_chat_id).toBe("chat-1");
    expect(response.hadCredentialRecoveryToken).toBe(true);
  });

  it.each([false, true])(
    "reports whether the successful retry carried a recovery token: %s",
    async (hadToken) => {
      if (!hadToken) ensureCredentialRecoveryToken("wpid-1");
      const get = vi
        .fn()
        .mockImplementationOnce(async () => {
          if (hadToken) ensureCredentialRecoveryToken("wpid-1");
          else sessionStorage.clear();
          throw new Error("temporary network failure");
        })
        .mockResolvedValueOnce({ data: {} });
      const client = { get } as unknown as Pick<AxiosInstance, "get">;

      const response = await readCredentialRecoveryHistory(
        client,
        "wpid-1",
        {},
        { retryTransientFailure: true },
      );

      expect(get).toHaveBeenCalledTimes(2);
      expect(
        Boolean(
          get.mock.calls[1]?.[1]?.headers[
            "X-Copilot-Credential-Recovery-Token"
          ],
        ),
      ).toBe(hadToken);
      expect(response.hadCredentialRecoveryToken).toBe(hadToken);
    },
  );
});
