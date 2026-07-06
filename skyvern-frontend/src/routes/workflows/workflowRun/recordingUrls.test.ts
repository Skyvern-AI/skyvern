import { describe, expect, it } from "vitest";

import { artifactApiBaseUrl } from "@/util/env";
import { getRecordingUrls } from "./recordingUrls";

describe("getRecordingUrls", () => {
  it("encodes file recording paths so the path query parameter round-trips", () => {
    const recordingPath = "/tmp/my recording&x#hash?percent=100%.mp4";

    const [recordingUrl] = getRecordingUrls({
      recording_urls: [`file://${recordingPath}`],
    });

    if (recordingUrl === undefined) {
      throw new Error("Expected a recording URL");
    }

    expect(recordingUrl).toBe(
      `${artifactApiBaseUrl}/artifact/recording?path=${encodeURIComponent(recordingPath)}`,
    );
    expect(new URL(recordingUrl).searchParams.get("path")).toBe(recordingPath);
  });
});
