import { validateJson, validateUrl } from "./httpValidation";
import { describe, test, expect } from "vitest";

describe("validateJson", () => {
  test("empty string is valid with null message", () => {
    expect(validateJson("")).toEqual({ valid: true, message: null });
  });

  test("whitespace-only string is valid with null message", () => {
    expect(validateJson("   ")).toEqual({ valid: true, message: null });
  });

  test("empty object is valid with null message", () => {
    expect(validateJson("{}")).toEqual({ valid: true, message: null });
  });

  test("empty array is valid with null message", () => {
    expect(validateJson("[]")).toEqual({ valid: true, message: null });
  });

  test("valid JSON object returns valid", () => {
    const result = validateJson('{"key": "value"}');
    expect(result.valid).toBe(true);
    expect(result.message).toBe("Valid JSON");
  });

  test("quoted template placeholder is valid JSON", () => {
    const result = validateJson('{"url": "{{ webhook_url }}"}');
    expect(result.valid).toBe(true);
    expect(result.message).toBe("Valid JSON");
  });

  test("multiple quoted templates are valid", () => {
    const result = validateJson(
      '{"first": "{{ first_name }}", "last": "{{ last_name }}"}',
    );
    expect(result.valid).toBe(true);
    expect(result.message).toBe("Valid JSON");
  });

  test("unquoted template placeholder is invalid with actionable message", () => {
    const result = validateJson('{"url": {{ webhook_url }}}');
    expect(result.valid).toBe(false);
    expect(result.message).toContain(
      "Template placeholders must be wrapped in quotes",
    );
  });

  test("unquoted template in value position is invalid", () => {
    const result = validateJson(
      '{"first": {{ patient_first_name }}, "last": "Doe"}',
    );
    expect(result.valid).toBe(false);
    expect(result.message).toContain(
      "Template placeholders must be wrapped in quotes",
    );
  });

  test("trailing comma is invalid", () => {
    const result = validateJson('{"key": "value",}');
    expect(result.valid).toBe(false);
  });

  test("completely malformed JSON is invalid", () => {
    const result = validateJson("{key: value}");
    expect(result.valid).toBe(false);
  });

  test("unclosed brace is invalid", () => {
    const result = validateJson('{"key": "value"');
    expect(result.valid).toBe(false);
  });

  test("valid JSON with quoted template and other values is valid", () => {
    const result = validateJson('{"inner": "{{ param }}", "num": 42}');
    expect(result.valid).toBe(true);
    expect(result.message).toBe("Valid JSON");
  });

  test("JSON array with valid content is valid", () => {
    const result = validateJson('["a", "b", "{{ param }}"]');
    expect(result.valid).toBe(true);
    expect(result.message).toBe("Valid JSON");
  });
});

describe("validateUrl", () => {
  test("empty URL is invalid", () => {
    const result = validateUrl("");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL is required");
  });

  test("valid http URL is valid", () => {
    const result = validateUrl("http://example.com");
    expect(result.valid).toBe(true);
  });

  test("valid https URL is valid", () => {
    const result = validateUrl("https://example.com/api");
    expect(result.valid).toBe(true);
  });

  test("non-http protocol is invalid", () => {
    const result = validateUrl("ftp://example.com");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("malformed URL is invalid", () => {
    const result = validateUrl("not a url");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("Invalid URL format");
  });
});
