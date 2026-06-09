import { describe, expect, it } from "vitest";

import {
  parseTagFilter,
  parseTagFilterTerm,
  parseTagInput,
  parseTypedTagQuery,
  serializeTagFilter,
  serializeTagFilterTerm,
  sortTags,
  tagElementKey,
  validateTagKey,
  validateTagValue,
  type Tag,
  type TagFilterTerm,
} from "./tagTypes";

describe("parseTagInput", () => {
  it("parses a bare label as a standalone tag", () => {
    expect(parseTagInput("production")).toEqual({
      key: null,
      value: "production",
    });
  });

  it("splits group:label on the first colon, keeping later colons in the value", () => {
    expect(parseTagInput("env:prod")).toEqual({ key: "env", value: "prod" });
    expect(parseTagInput("url:http://x:8000")).toEqual({
      key: "url",
      value: "http://x:8000",
    });
  });

  it("trims surrounding whitespace", () => {
    expect(parseTagInput("  env : prod  ")).toEqual({
      key: "env",
      value: "prod",
    });
  });

  it("returns null for empty or partial input", () => {
    expect(parseTagInput("")).toBeNull();
    expect(parseTagInput("   ")).toBeNull();
    expect(parseTagInput("env:")).toBeNull();
    expect(parseTagInput(":prod")).toBeNull();
  });
});

describe("parseTagFilterTerm", () => {
  it("parses the three term shapes", () => {
    expect(parseTagFilterTerm("prod")).toEqual({ key: null, value: "prod" });
    expect(parseTagFilterTerm("env:*")).toEqual({ key: "env", value: null });
    expect(parseTagFilterTerm("env:prod")).toEqual({
      key: "env",
      value: "prod",
    });
  });

  it("returns null for malformed terms", () => {
    expect(parseTagFilterTerm("")).toBeNull();
    expect(parseTagFilterTerm(":prod")).toBeNull();
    expect(parseTagFilterTerm("env:")).toBeNull();
  });
});

describe("serializeTagFilterTerm", () => {
  it("serializes each shape", () => {
    expect(serializeTagFilterTerm({ key: null, value: "prod" })).toBe("prod");
    expect(serializeTagFilterTerm({ key: "env", value: null })).toBe("env:*");
    expect(serializeTagFilterTerm({ key: "env", value: "prod" })).toBe(
      "env:prod",
    );
  });
});

describe("serializeTagFilter", () => {
  it("sorts standalone first, then by key, then value", () => {
    const terms: Array<TagFilterTerm> = [
      { key: "env", value: "prod" },
      { key: null, value: "beta" },
      { key: "env", value: null },
      { key: null, value: "alpha" },
    ];
    expect(serializeTagFilter(terms)).toBe("alpha,beta,env:*,env:prod");
  });

  it("drops a degenerate empty term instead of emitting stray commas", () => {
    // {key:null, value:null} is never produced by parseTagFilterTerm; this guards
    // the serializer against it if the type is ever widened.
    const terms: Array<TagFilterTerm> = [
      { key: null, value: null },
      { key: null, value: "x" },
    ];
    expect(serializeTagFilter(terms)).toBe("x");
  });

  it("is order-independent", () => {
    const a: Array<TagFilterTerm> = [
      { key: "env", value: "prod" },
      { key: null, value: "x" },
    ];
    const b: Array<TagFilterTerm> = [
      { key: null, value: "x" },
      { key: "env", value: "prod" },
    ];
    expect(serializeTagFilter(a)).toBe(serializeTagFilter(b));
  });
});

describe("parseTagFilter", () => {
  it("round-trips a canonical string", () => {
    const raw = "alpha,beta,env:*,env:prod";
    expect(serializeTagFilter(parseTagFilter(raw))).toBe(raw);
  });

  it("dedupes repeats and drops blank segments", () => {
    expect(parseTagFilter("x,x, ,env:*")).toEqual([
      { key: null, value: "x" },
      { key: "env", value: null },
    ]);
  });

  it("returns [] for empty input", () => {
    expect(parseTagFilter(null)).toEqual([]);
    expect(parseTagFilter("")).toEqual([]);
  });
});

describe("sortTags", () => {
  it("orders standalone labels first, then grouped by key then value", () => {
    const tags: Array<Tag> = [
      { key: "env", value: "prod" },
      { key: null, value: "b" },
      { key: "env", value: "dev" },
      { key: null, value: "a" },
    ];
    expect(sortTags(tags)).toEqual([
      { key: null, value: "a" },
      { key: null, value: "b" },
      { key: "env", value: "dev" },
      { key: "env", value: "prod" },
    ]);
  });
});

describe("tagElementKey", () => {
  it("namespaces standalone vs grouped so a null key can't collide with an empty key", () => {
    expect(tagElementKey({ key: null, value: "foo" })).toBe("label:foo");
    expect(tagElementKey({ key: "", value: "foo" })).toBe("group::foo");
    expect(tagElementKey({ key: null, value: "foo" })).not.toBe(
      tagElementKey({ key: "", value: "foo" }),
    );
  });
});

describe("parseTypedTagQuery", () => {
  it("returns a null key for a bare query", () => {
    expect(parseTypedTagQuery("prod")).toEqual({
      typedKey: null,
      typedValuePartial: "",
    });
  });

  it("splits group:partial and lowercases the value fragment", () => {
    expect(parseTypedTagQuery("env:PR")).toEqual({
      typedKey: "env",
      typedValuePartial: "pr",
    });
  });

  it("treats a leading colon as no group", () => {
    expect(parseTypedTagQuery(":prod")).toEqual({
      typedKey: null,
      typedValuePartial: "",
    });
  });
});

describe("tag validation", () => {
  it("rejects a colon in a standalone label but allows it in a grouped value", () => {
    expect(validateTagValue("a:b", { hasKey: false })).not.toBeNull();
    expect(validateTagValue("a:b", { hasKey: true })).toBeNull();
  });

  it("rejects a grouped value of exactly the wildcard", () => {
    expect(validateTagValue("*", { hasKey: true })).not.toBeNull();
  });

  it("rejects an empty key and the reserved prefix", () => {
    expect(validateTagKey("")).not.toBeNull();
    expect(validateTagKey("skyvern.foo")).not.toBeNull();
    expect(validateTagKey("env")).toBeNull();
  });
});
