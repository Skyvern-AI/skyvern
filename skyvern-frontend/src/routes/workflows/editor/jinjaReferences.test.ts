import { describe, expect, test } from "vitest";

import {
  containsJinjaReference,
  removeJinjaReference,
  replaceJinjaReference,
} from "./jinjaReferences";

describe("replaceJinjaReference", () => {
  test("renames simple reference and preserves inner whitespace", () => {
    expect(replaceJinjaReference("hi {{ key }}", "key", "newKey")).toBe(
      "hi {{ newKey }}",
    );
    expect(replaceJinjaReference("hi {{key}}", "key", "newKey")).toBe(
      "hi {{newKey}}",
    );
  });

  test("renames reference with field accessor and filter args", () => {
    expect(
      replaceJinjaReference("{{ key.field | filter('a') }}", "key", "k2"),
    ).toBe("{{ k2.field | filter('a') }}");
  });

  test("does not rename a longer key with shared prefix", () => {
    expect(replaceJinjaReference("{{ keyOther }}", "key", "x")).toBe(
      "{{ keyOther }}",
    );
  });
});

describe("removeJinjaReference", () => {
  test("removes a standalone reference and preserves outer whitespace", () => {
    expect(removeJinjaReference("hi {{ key }} bye", "key")).toBe("hi bye");
  });

  test("collapses a newline gap between two paragraphs", () => {
    expect(removeJinjaReference("para 1\n{{ key }}\npara 2", "key")).toBe(
      "para 1\npara 2",
    );
  });

  test("preserves intentional double-newline paragraph breaks elsewhere", () => {
    const input = "first\n\nsecond {{ key }} third\n\nfourth";
    expect(removeJinjaReference(input, "key")).toBe(
      "first\n\nsecond third\n\nfourth",
    );
  });

  test("removes references whose interior contains a single `}` literal", () => {
    expect(removeJinjaReference("a {{ key | default('{}') }} b", "key")).toBe(
      "a b",
    );
  });

  test("matches references with a field path and a filter pipeline", () => {
    expect(
      removeJinjaReference("x {{ key.field | filter('a') }} y", "key"),
    ).toBe("x y");
  });

  test("does not match a longer key sharing a prefix", () => {
    expect(removeJinjaReference("{{ keyOther }}", "key")).toBe(
      "{{ keyOther }}",
    );
  });

  test("returns empty string when text is exactly the reference", () => {
    expect(removeJinjaReference("{{ key }}", "key")).toBe("");
  });

  test("noop when key is absent", () => {
    expect(removeJinjaReference("hello world", "key")).toBe("hello world");
  });
});

describe("containsJinjaReference", () => {
  test("matches a basic reference", () => {
    expect(containsJinjaReference("hi {{ key }}", "key")).toBe(true);
  });

  test("does not match a longer key sharing a prefix", () => {
    expect(containsJinjaReference("hi {{ keyOther }}", "key")).toBe(false);
  });

  test("matches a reference with a filter expression", () => {
    expect(containsJinjaReference("hi {{ key | upper }}", "key")).toBe(true);
  });
});
