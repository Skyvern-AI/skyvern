import { describe, expect, test } from "vitest";
import { getBlockDownloadedFileUrls } from "./blockDownloadedFiles";

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
