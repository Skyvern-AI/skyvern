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

  test("non-breaking spaces outside strings explain the hidden character", () => {
    const result = validateJson(`{
  "personalInfo": {
    "firstName": "Luis",
    "lastName": "Ortiz"\u00a0
  }
}`);
    expect(result.valid).toBe(false);
    expect(result.message).toContain(
      "JSON contains a non-breaking space (U+00A0) at line 4, column 24; replace it with a regular space.",
    );
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

  test("template-only URL is valid", () => {
    const result = validateUrl("{{login_complete_url}}");
    expect(result.valid).toBe(true);
  });

  test("template-only URL with inner spaces is valid", () => {
    const result = validateUrl("{{ login_complete_url }}");
    expect(result.valid).toBe(true);
  });

  test("template URL with a path is valid", () => {
    const result = validateUrl("{{base_url}}/api/callback");
    expect(result.valid).toBe(true);
  });

  test("https URL with a template host is valid", () => {
    const result = validateUrl("https://{{host}}/path");
    expect(result.valid).toBe(true);
  });

  test("template control expression URL is valid", () => {
    const result = validateUrl("{% if use_prod %}https://a.com{% endif %}");
    expect(result.valid).toBe(true);
  });

  test("non-http protocol is invalid", () => {
    const result = validateUrl("ftp://example.com");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("non-http protocol with a template host is invalid", () => {
    const result = validateUrl("ftp://{{host}}");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("javascript scheme with a template payload is invalid", () => {
    const result = validateUrl("javascript:{{payload}}");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("space-spliced javascript scheme with a template payload is invalid", () => {
    const result = validateUrl("java script:{{payload}}");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("tab-spliced javascript scheme with a template payload is invalid", () => {
    const result = validateUrl("java\tscript:{{payload}}");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("mailto scheme with a template address is invalid", () => {
    const result = validateUrl("mailto:{{address}}");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("URL must use HTTP or HTTPS protocol");
  });

  test("scheme-relative URL with a template host is invalid", () => {
    const result = validateUrl("//{{host}}/evil.js");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("Invalid URL format");
  });

  test("malformed URL is invalid", () => {
    const result = validateUrl("not a url");
    expect(result.valid).toBe(false);
    expect(result.message).toBe("Invalid URL format");
  });
});
