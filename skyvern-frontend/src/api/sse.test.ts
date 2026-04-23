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
});
