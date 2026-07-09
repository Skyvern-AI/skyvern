import { afterEach, describe, expect, it, vi } from "vitest";

import {
  normalizeWorkflowTags,
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
  it.each([
    [
      "parses a bare label as a standalone tag",
      [["production", { key: null, value: "production" }]],
    ],
    [
      "splits group:label on the first colon, keeping later colons in the value",
      [
        ["env:prod", { key: "env", value: "prod" }],
        ["url:http://x:8000", { key: "url", value: "http://x:8000" }],
      ],
    ],
    [
      "trims surrounding whitespace",
      [["  env : prod  ", { key: "env", value: "prod" }]],
    ],
  ] as const)("%s", (_, cases) => {
    for (const [raw, expected] of cases) {
      expect(parseTagInput(raw)).toEqual(expected);
    }
  });

  it("returns null for empty or partial input", () => {
    for (const raw of ["", "   ", "env:", ":prod"]) {
      expect(parseTagInput(raw)).toBeNull();
    }
  });
});

describe("parseTagFilterTerm", () => {
  it("parses the three term shapes", () => {
    for (const [raw, expected] of [
      ["prod", { key: null, value: "prod" }],
      ["env:*", { key: "env", value: null }],
      ["env:prod", { key: "env", value: "prod" }],
    ] as const) {
      expect(parseTagFilterTerm(raw)).toEqual(expected);
    }
  });

  it("returns null for malformed terms", () => {
    for (const raw of ["", ":prod", "env:"]) {
      expect(parseTagFilterTerm(raw)).toBeNull();
    }
  });
});

describe("serializeTagFilterTerm", () => {
  it("serializes each shape", () => {
    for (const [term, expected] of [
      [{ key: null, value: "prod" }, "prod"],
      [{ key: "env", value: null }, "env:*"],
      [{ key: "env", value: "prod" }, "env:prod"],
    ] as const) {
      expect(serializeTagFilterTerm(term)).toBe(expected);
    }
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
    for (const raw of [null, ""]) {
      expect(parseTagFilter(raw)).toEqual([]);
    }
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

describe("normalizeWorkflowTags", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("passes a well-formed tag list through unchanged", () => {
    const tags: Array<Tag> = [
      { key: null, value: "prod" },
      { key: "env", value: "dev" },
    ];
    expect(normalizeWorkflowTags(tags)).toEqual(tags);
  });

  it("drops malformed list entries and warns instead of throwing", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const skewed = [
      { key: "env", value: "prod" },
      { key: "bad", value: { key: "nested", value: "object" } },
      { value: 42 },
      null,
      "loose-string",
    ];
    expect(normalizeWorkflowTags(skewed)).toEqual([
      { key: "env", value: "prod" },
    ]);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("converts a legacy key-to-value record and warns", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(normalizeWorkflowTags({ env: "prod", team: "growth" })).toEqual([
      { key: "env", value: "prod" },
      { key: "team", value: "growth" },
    ]);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("degrades null, undefined, and junk to an empty list without throwing", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(normalizeWorkflowTags(null)).toEqual([]);
    expect(normalizeWorkflowTags(undefined)).toEqual([]);
    expect(normalizeWorkflowTags("env:prod")).toEqual([]);
    expect(normalizeWorkflowTags(7)).toEqual([]);
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
  it.each([
    [
      "returns a null key for a bare query",
      "prod",
      { typedKey: null, typedValuePartial: "" },
    ],
    [
      "splits group:partial and lowercases the value fragment",
      "env:PR",
      { typedKey: "env", typedValuePartial: "pr" },
    ],
    [
      "treats a leading colon as no group",
      ":prod",
      { typedKey: null, typedValuePartial: "" },
    ],
  ] as const)("%s", (_, raw, expected) => {
    expect(parseTypedTagQuery(raw)).toEqual(expected);
  });
});

describe("tag validation", () => {
  it("rejects a colon in a standalone label but allows it in a grouped value", () => {
    for (const [hasKey, expected] of [
      [false, "invalid"],
      [true, "valid"],
    ] as const) {
      const result = validateTagValue("a:b", { hasKey });
      if (expected === "valid") {
        expect(result).toBeNull();
      } else {
        expect(result).not.toBeNull();
      }
    }
  });

  it("rejects a grouped value of exactly the wildcard", () => {
    expect(validateTagValue("*", { hasKey: true })).not.toBeNull();
  });

  it("rejects an empty key and the reserved prefix", () => {
    for (const key of ["", "skyvern.foo"]) {
      expect(validateTagKey(key)).not.toBeNull();
    }
    expect(validateTagKey("env")).toBeNull();
  });
});
