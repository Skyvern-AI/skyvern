import { describe, expect, it } from "vitest";

import { isBrowserFetchableUrl } from "./fileUploadLink";

describe("upload download links", () => {
  it("links an address the browser can fetch and nothing else", () => {
    expect(
      isBrowserFetchableUrl("https://files.example.com/o_1/report.csv"),
    ).toBe(true);
    expect(isBrowserFetchableUrl("http://localhost:8000/files/file_1")).toBe(
      true,
    );
    // Local storage answers with the server's own path; an https page cannot read it.
    expect(
      isBrowserFetchableUrl(
        "file:///srv/artifacts/local/o_1/2026-09-12/file_1_report.csv",
      ),
    ).toBe(false);
  });
});
