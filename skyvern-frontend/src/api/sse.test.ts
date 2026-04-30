import { describe, it, expect, vi } from "vitest";
import type { FetchEventSourceInit } from "@microsoft/fetch-event-source";

const mockFetchEventSource = vi.fn(
  (_url: string, opts: FetchEventSourceInit) => {
    // Send a message so the inner promise can settle
    opts.onmessage?.({
      id: "",
      event: "done",
      data: '{"done":true}',
      retry: undefined,
    });
    return Promise.resolve();
  },
);

vi.mock("@microsoft/fetch-event-source", () => ({
  fetchEventSource: mockFetchEventSource,
}));

const { fetchStreamingSse } = await import("./sse");

describe("fetchStreamingSse", () => {
  it("passes openWhenHidden: true to fetchEventSource", async () => {
    await fetchStreamingSse(
      "http://localhost/test",
      { method: "POST", headers: {}, body: "{}" },
      () => true, // return true to resolve the promise on first message
    );

    expect(mockFetchEventSource).toHaveBeenCalledWith(
      "http://localhost/test",
      expect.objectContaining({ openWhenHidden: true }),
    );
  });

  it("resolves wrapper when fetchEventSource exits cleanly under user abort", async () => {
    const externalController = new AbortController();
    mockFetchEventSource.mockImplementationOnce((_url, opts) => {
      // Library forwards external abort to its internal signal then returns
      // void — no onerror, no onmessage. Without the .then handler the
      // wrapper Promise would hang.
      opts.signal?.addEventListener("abort", () => {}, { once: true });
      externalController.abort();
      return Promise.resolve();
    });

    const settled = await Promise.race([
      fetchStreamingSse(
        "http://localhost/test",
        { method: "POST", headers: {}, body: "{}" },
        () => false,
        { signal: externalController.signal },
      ).then(() => "resolved" as const),
      new Promise<"timeout">((r) => setTimeout(() => r("timeout"), 100)),
    ]);
    expect(settled).toBe("resolved");
  });

  it("rejects wrapper when stream closes cleanly without a terminal event", async () => {
    mockFetchEventSource.mockImplementationOnce(() => Promise.resolve());

    await expect(
      fetchStreamingSse(
        "http://localhost/test",
        { method: "POST", headers: {}, body: "{}" },
        () => false,
      ),
    ).rejects.toThrow(/terminal event/i);
  });
});
