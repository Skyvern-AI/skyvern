import { describe, expect, test } from "vitest";

import {
  getInvalidJsonMessage,
  getJsonParseErrorDetail,
} from "./jsonParseError";

describe("json parse error formatting", () => {
  test("explains hidden non-breaking spaces outside strings", () => {
    const value = `{
  "personalInfo": {
    "firstName": "Luis",
    "lastName": "Ortiz"\u00a0
  }
}`;
    let error: unknown;

    try {
      JSON.parse(value);
    } catch (caught) {
      error = caught;
    }

    expect(getInvalidJsonMessage(value, error)).toContain(
      "Invalid JSON: JSON contains a non-breaking space (U+00A0) at line 4, column 24; replace it with a regular space.",
    );
  });

  test("preserves parser details for ordinary JSON syntax errors", () => {
    const value = '{"first": "Luis",}';
    let error: unknown;

    try {
      JSON.parse(value);
    } catch (caught) {
      error = caught;
    }

    expect(getJsonParseErrorDetail(value, error)).toMatch(
      /Expected double-quoted property name|Unexpected token/,
    );
  });

  test("adds line and column when the parser only reports a position", () => {
    const value = '{\n  "first": "Luis",\n}';

    expect(
      getJsonParseErrorDetail(
        value,
        "Unexpected token } in JSON at position 21",
      ),
    ).toBe("Unexpected token } in JSON at position 21 (line 3 column 1)");
  });

  test("does not flag non-breaking spaces inside strings", () => {
    const value = '{"lastName": "Ortiz\u00a0"}';

    expect(getJsonParseErrorDetail(value, "Parse error")).toBe("Parse error");
  });
});
