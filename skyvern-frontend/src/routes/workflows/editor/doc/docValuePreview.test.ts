import { describe, expect, test } from "vitest";
import { docInputValuePreview } from "./docValuePreview";

const LONG = "https://example.com/a/very/long/path/to/some/page?query=1";
const ORDER = { customer_ref: "c1", amount: 3, currency: "USD", note: "" };
const UPLOAD =
  "s3://uploads-bucket/production/o_1/2026-11-10/passport%20scan.pdf";
const SPACED = "s3://bkt/prod/o_1/2026-11-10/fid_My Passport.jpg";
const FREE_TEXT = ["ftp://host/a and/or b, please check", "s3://b/dir/ "];

describe("docInputValuePreview", () => {
  test.each([
    [
      "a URL to a file",
      "https://example.com/uploads/2026/passport.jpg",
      "example.com/…/passport.jpg",
    ],
    ["a URL without a path", "https://example.com/", "example.com"],
    ["a one-segment URL", "https://example.com/form", "example.com/form"],
    ["a JSON object", ORDER, "{customer_ref, amount, +2}"],
    [
      "a JSON object typed as text",
      JSON.stringify(ORDER),
      "{customer_ref, amount, +2}",
    ],
    ["an array", [1, 2, 3], "[3 items]"],
    ["plain text", "hello world", "hello world"],
    [
      "a URL with a template",
      "https://example.com/{{ order_id }}",
      "example.com/{{ order_id }}",
    ],
    ["an uploaded file", UPLOAD, "passport scan.pdf"],
    [
      "a storage URI without a path",
      "s3://uploads-bucket/",
      "s3://uploads-bucket",
    ],
  ])("%s", (_, value, text) => {
    expect(docInputValuePreview(value)?.text).toBe(text);
  });

  test("keeps the full value in the title and shows nothing for an empty value", () => {
    expect(docInputValuePreview(LONG)?.title).toBe(LONG);
    expect(docInputValuePreview(ORDER)?.title).toContain('"currency": "USD"');
    expect(docInputValuePreview(UPLOAD)).toEqual({
      text: "passport scan.pdf",
      title: UPLOAD,
    });
    expect(docInputValuePreview("")).toBeNull();
  });

  test("names an uploaded file whose name has a space, but not a host with one", () => {
    expect(docInputValuePreview(SPACED)).toEqual({
      text: "fid_My Passport.jpg",
      title: SPACED,
    });
    expect(docInputValuePreview("s3://my bucket/a.jpg")).toEqual({
      text: "s3://my bucket/a.jpg",
      title: "s3://my bucket/a.jpg",
    });
  });

  test.each([
    "accounts.payable@northwind-traders.com",
    "hello world",
    "https://example.com/{{ order_id }}",
    ...FREE_TEXT,
  ])(
    "the chip cuts by width, so %s keeps the whole value as its title",
    (value) => {
      expect(docInputValuePreview(value)?.title).toBe(value);
    },
  );

  test("text after a scheme reads as the whole value; a long storage URI gets a capped title", () => {
    for (const value of FREE_TEXT) {
      expect(docInputValuePreview(value)?.text).toBe(value);
    }
    const long = `s3://bkt/${"d/".repeat(1500)}scan.pdf`;
    const title = docInputValuePreview(long)?.title;
    expect(docInputValuePreview(long)?.text).toBe("scan.pdf");
    expect(title).toHaveLength(2001);
    expect(title?.endsWith("…")).toBe(true);
  });
});
