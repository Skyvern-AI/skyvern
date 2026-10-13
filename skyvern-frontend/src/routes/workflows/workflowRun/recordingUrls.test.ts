import { describe, expect, it } from "vitest";

import { getRecordingUrls } from "./recordingUrls";

describe("getRecordingUrls", () => {
  it("returns an empty array for missing input", () => {
    expect(getRecordingUrls(null)).toEqual([]);
    expect(getRecordingUrls(undefined)).toEqual([]);
    expect(getRecordingUrls({})).toEqual([]);
  });

  it("passes non-file URLs through unchanged", () => {
    const url = "https://example.com/recording.mp4";
    expect(getRecordingUrls({ recording_urls: [url] })).toEqual([url]);
  });

  it("percent-encodes the file path in the query so special characters survive", () => {
    const urls = getRecordingUrls({
      recording_urls: ["file:///tmp/my recording&x.mp4"],
    });
    expect(urls).toHaveLength(1);
    // The whole path must be one encoded query value, not split on the raw "&".
    expect(urls[0]).toContain(
      "path=" + encodeURIComponent("/tmp/my recording&x.mp4"),
    );
    // A parser must recover the original path intact.
    const parsed = new URL(urls[0]!);
    expect(parsed.searchParams.get("path")).toBe("/tmp/my recording&x.mp4");
  });

  it("encodes a plain path the same way its task-detail sibling does", () => {
    const urls = getRecordingUrls({
      recording_url: "file:///tmp/recording.mp4",
    });
    expect(urls[0]).toContain(
      "path=" + encodeURIComponent("/tmp/recording.mp4"),
    );
  });

  it("prefers recording_urls over recording_url", () => {
    const urls = getRecordingUrls({
      recording_urls: ["https://example.com/a.mp4"],
      recording_url: "https://example.com/b.mp4",
    });
    expect(urls).toEqual(["https://example.com/a.mp4"]);
  });
});
