import { describe, expect, test } from "vitest";
import { docCellPreview, docInputValuePreview } from "./docValuePreview";

const LONG = "https://example.com/a/very/long/path/to/some/page?query=1";
const ORDER = { customer_ref: "c1", amount: 3, currency: "USD", note: "" };
const UPLOAD =
  "s3://uploads-bucket/production/o_1/2026-11-10/passport%20scan.pdf";
const ARTIFACT =
  "https://api.example.com/v1/artifacts/a_1/content?expiry=1&artifact_name=invoice.pdf&signature=x";

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
});

describe("docCellPreview", () => {
  test.each([
    [
      "a URL to a file",
      "https://example.com/files/passport.jpg",
      { text: "passport.jpg", href: "https://example.com/files/passport.jpg" },
    ],
    [
      "a URL without a path",
      "https://example.com/",
      { text: "https://example.com", href: "https://example.com/" },
    ],
    [
      "a long URL",
      LONG,
      { text: "https://example.com/a/very/long/path/to/…", href: LONG },
    ],
    ["a JSON object", ORDER, { text: "{customer_ref, amount, +2}" }],
    [
      "a short object",
      { pages: 2, kind: "scan" },
      { text: '{"pages":2,"kind":"scan"}' },
    ],
    ["a short array", ["red", "blue"], { text: '["red","blue"]' }],
    [
      "a long array",
      Array.from({ length: 20 }, (_, n) => n),
      { text: "[20 items]" },
    ],
    [
      "a Skyvern artifact URL",
      ARTIFACT,
      { text: "invoice.pdf", href: ARTIFACT },
    ],
    ["an uploaded file", UPLOAD, { text: "passport scan.pdf" }],
    ["plain text", "hello world", { text: "hello world" }],
    [
      "a URL with a template",
      "https://example.com/{{ id }}",
      { text: "https://example.com/{{ id }}" },
    ],
  ])("%s", (_, value, expected) => {
    const preview = docCellPreview(value);
    expect({ text: preview.text, href: preview.href }).toEqual({
      href: undefined,
      ...expected,
    });
  });

  test("a long value keeps its full value in a title capped at 2,000 characters", () => {
    expect(docCellPreview(ORDER).title).toContain('"currency": "USD"');
    const title = docCellPreview(
      Array.from({ length: 2000 }, (_, n) => ({ n })),
    ).title;
    expect(title).toHaveLength(2001);
    expect(title?.endsWith("…")).toBe(true);
    expect(docCellPreview(UPLOAD).title).toBe(UPLOAD);
  });

  test("two links that differ only by query read differently", () => {
    const one = docCellPreview("https://example.com/files/download.php?id=1");
    const two = docCellPreview("https://example.com/files/download.php?id=2");
    expect([one.text, two.text]).toEqual([
      "download.php?id=1",
      "download.php?id=2",
    ]);
  });
});
