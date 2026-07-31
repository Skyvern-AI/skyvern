import { describe, expect, it } from "vitest";

import { safeHttpUrl } from "./httpUrl";

describe("safeHttpUrl", () => {
  it.each(["http://example.com/path", "https://example.com/path?token=abc"])(
    "accepts %s",
    (value) => {
      expect(safeHttpUrl(value)).toBe(value);
    },
  );

  it.each([
    "javascript:alert(1)",
    "data:text/html,<h1>unsafe</h1>",
    "ftp://example.com/file",
    "not a url",
  ])("rejects %s", (value) => {
    expect(safeHttpUrl(value)).toBeNull();
  });
});
