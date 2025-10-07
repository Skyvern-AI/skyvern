// TSON.parse.test.ts
import { TSON } from "./tson";
import { describe, test, expect } from "vitest";

describe("TSON.parse", () => {
  test("single top-level template works", () => {
    const input = "{{ hello }}";
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual("<STUB>");
  });

  test("preserves double braces inside quoted strings", () => {
    const input = '{"a": "{{ hello }}"}';
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual({ a: "{{ hello }}" });
  });

  test("replaces double braces outside strings with stub", () => {
    const input = '{"a": {{ hello }} }';
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual({ a: "<STUB>" });
  });

  test("handles double braces in keys and values", () => {
    const input = `
{
    "hello": "world",
    {{foo}}: "bar",
    "baz": {{quux}}
}`;
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual({
      hello: "world",
      "<STUB>": "bar",
      baz: "<STUB>",
    });
  });

  test("does not allow trailing commas", () => {
    const input = `
{
    "hello": "world",
    {{foo}}: "bar",
    "baz": {{quux}},
}`;
    const result = TSON.parse(input);

    expect(result.success).toBe(false);
    expect(result.error).toContain("Expected double-quoted property name");
  });

  test("detects unclosed double braces", () => {
    const input = "{{ unclosed";
    const result = TSON.parse(input);

    expect(result.success).toBe(false);
    expect(result.error).toContain("Unclosed");
  });

  test("detects unmatched closing double braces", () => {
    const input = "closed }}";
    const result = TSON.parse(input);

    expect(result.success).toBe(false);
    expect(result.error).toContain("Unmatched");
  });

  test("handles nested double braces", () => {
    const input = "{{ {{ nested }} }}";
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual("<STUB>");
  });

  test("handles double braces in arrays", () => {
    const input = '[{{ }}, {{ }}, "normal"]';
    const result = TSON.parse(input);

    expect(result.success).toBe(true);
    expect(result.data).toEqual(["<STUB>", "<STUB>", "normal"]);
  });
});
