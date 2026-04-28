import { describe, expect, test } from "vitest";
import {
  filenameForDownloadedFileUrl,
  getBlockDownloadedFileUrls,
} from "./blockDownloadedFiles";

describe("filenameForDownloadedFileUrl", () => {
  test("short signed URL: pulls filename from artifact_name query param", () => {
    const url =
      "https://api.skyvern.com/v1/artifacts/a_123/content" +
      "?expiry=1&kid=k&sig=s&artifact_name=invoice-2026.pdf&artifact_type=download";
    expect(filenameForDownloadedFileUrl(url)).toBe("invoice-2026.pdf");
  });

  test("short signed URL with non-ASCII filename: round-trips via URL decoding", () => {
    const url =
      "https://api.skyvern.com/v1/artifacts/a_123/content" +
      "?expiry=1&kid=k&sig=s&artifact_name=" +
      encodeURIComponent("\u62a5\u544a-2026.pdf");
    expect(filenameForDownloadedFileUrl(url)).toBe("报告-2026.pdf");
  });

  test("legacy S3 presigned URL: pulls filename from path basename", () => {
    const url =
      "https://skyvern-uploads.s3.amazonaws.com/" +
      "downloads/production/o_1/wr_1/legacy-report.pdf" +
      "?AWSAccessKeyId=ASIA&Signature=sig&Expires=1234567890";
    expect(filenameForDownloadedFileUrl(url)).toBe("legacy-report.pdf");
  });

  test("legacy S3 presigned URL with percent-encoded filename: decodes the basename", () => {
    const url =
      "https://skyvern-uploads.s3.amazonaws.com/" +
      "downloads/production/o_1/wr_1/Q2%20report.pdf" +
      "?AWSAccessKeyId=x&Signature=y";
    expect(filenameForDownloadedFileUrl(url)).toBe("Q2 report.pdf");
  });

  test("short URL without artifact_name and trailing /content: returns 'download'", () => {
    const url = "https://api.skyvern.com/v1/artifacts/a_123/content?sig=x";
    expect(filenameForDownloadedFileUrl(url)).toBe("download");
  });

  test("malformed URL: returns 'download'", () => {
    expect(filenameForDownloadedFileUrl("not a url")).toBe("download");
  });
});

describe("getBlockDownloadedFileUrls", () => {
  test("returns [] when blockOutput is null/undefined/string/array", () => {
    expect(getBlockDownloadedFileUrls(null, [])).toEqual([]);
    expect(getBlockDownloadedFileUrls(undefined, [])).toEqual([]);
    expect(getBlockDownloadedFileUrls("str", [])).toEqual([]);
    expect(getBlockDownloadedFileUrls([1, 2], [])).toEqual([]);
  });

  test("returns [] when downloaded_file_urls is missing or not an array", () => {
    expect(getBlockDownloadedFileUrls({}, [])).toEqual([]);
    expect(
      getBlockDownloadedFileUrls({ downloaded_file_urls: "nope" }, []),
    ).toEqual([]);
  });

  test("filters non-string elements out of the persisted list", () => {
    const output = {
      downloaded_file_urls: [
        "https://s3/a/f.pdf?sig=old",
        null,
        42,
        "https://s3/b/g.pdf?sig=old",
      ],
    };
    expect(getBlockDownloadedFileUrls(output, [])).toEqual([
      "https://s3/a/f.pdf?sig=old",
      "https://s3/b/g.pdf?sig=old",
    ]);
  });

  test("swaps expired block URLs for fresh run-level URLs by path", () => {
    const output = {
      downloaded_file_urls: [
        "https://s3/a/f.pdf?sig=expired",
        "https://s3/b/g.pdf?sig=expired",
      ],
    };
    const fresh = [
      "https://s3/a/f.pdf?sig=fresh",
      "https://s3/b/g.pdf?sig=fresh",
      "https://s3/c/unrelated.pdf?sig=fresh",
    ];
    expect(getBlockDownloadedFileUrls(output, fresh)).toEqual([
      "https://s3/a/f.pdf?sig=fresh",
      "https://s3/b/g.pdf?sig=fresh",
    ]);
  });

  test("falls back to the persisted URL when no fresh match exists", () => {
    const output = {
      downloaded_file_urls: ["https://s3/a/f.pdf?sig=expired"],
    };
    expect(getBlockDownloadedFileUrls(output, [])).toEqual([
      "https://s3/a/f.pdf?sig=expired",
    ]);
  });

  test("preserves block order and scope even when fresh list has more entries", () => {
    const output = {
      downloaded_file_urls: ["https://s3/b/g.pdf?sig=old"],
    };
    const fresh = [
      "https://s3/a/f.pdf?sig=fresh",
      "https://s3/b/g.pdf?sig=fresh",
      "https://s3/c/h.pdf?sig=fresh",
    ];
    expect(getBlockDownloadedFileUrls(output, fresh)).toEqual([
      "https://s3/b/g.pdf?sig=fresh",
    ]);
  });
});
