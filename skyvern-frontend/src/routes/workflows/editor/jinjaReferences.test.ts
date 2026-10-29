import { describe, expect, test } from "vitest";

import {
  classifyUse,
  codeBlockUses,
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

describe("codeBlockUses", () => {
  test("lists declared inputs first, then {{ }} names in order of first use", () => {
    expect(
      codeBlockUses(
        ["region", "start_date"],
        "rows = {{ fetch_rows_output.rows }}\nfor r in {{ fetch_rows | tojson }}:\n    print({{ region }}, {{ cutoff }})",
      ),
    ).toEqual([
      "region",
      "start_date",
      "fetch_rows_output",
      "fetch_rows",
      "cutoff",
    ]);
  });

  test("keeps root names only: no attributes, filters, tests, kwargs or strings", () => {
    expect(
      codeBlockUses(
        [],
        "{{ a.b }} {{ c | default(d, boolean=true) }} {{ e['key'] }} {{ f is defined }} {{ 'g' ~ \"h\" }}",
      ),
    ).toEqual(["a", "c", "d", "e", "f"]);
  });

  test("skips names read only by a tag, as the AI fallback does", () => {
    expect(
      codeBlockUses(
        [],
        "{% if tax_id %}x{% endif %}{% for tax_id in [1] %}{{ tax_id }}{% endfor %}{{ real }}",
      ),
    ).toEqual(["real"]);
  });

  test("binds a for target until its endfor", () => {
    expect(
      codeBlockUses(
        [],
        "{{ rows }}{% for rows in other %}{{ rows }}{{ loop.index }}{% endfor %}{{ rows }}",
      ),
    ).toEqual(["rows"]);
  });

  test("a top-level set binds from its tag onward", () => {
    expect(
      codeBlockUses([], "{{ total }}{%- set total = base + 1 -%}{{ total }}"),
    ).toEqual(["total"]);
  });

  test("a set inside an if or for does not bind", () => {
    expect(
      codeBlockUses(
        [],
        "{% if false %}{% set rows = [] %}{% endif %}{{ rows }}",
      ),
    ).toEqual(["rows"]);
    expect(
      codeBlockUses(
        [],
        "{% for x in xs %}{% set count = 1 %}{% endfor %}{{ count }}",
      ),
    ).toEqual(["count"]);
  });

  test("ignores names inside a raw block", () => {
    expect(
      codeBlockUses(
        [],
        "{% raw %}{{ inside_raw }}{% endraw %}{{ real }}{%- raw -%}{{ also_raw }}{%- endraw %}",
      ),
    ).toEqual(["real"]);
  });

  test("ignores Jinja inside Python comments but not inside Python strings", () => {
    expect(codeBlockUses([], "# old {{ old_input }}")).toEqual([]);
    expect(
      codeBlockUses(
        [],
        'url = "https://example.com/#{{ page }}"  # was {{ legacy }}',
      ),
    ).toEqual(["page"]);
  });
});

describe("classifyUse", () => {
  const inputs = new Set(["region"]);
  const blocks = new Set(["fetch_rows"]);

  test("marks inputs, block labels and <label>_output, and neither", () => {
    expect(classifyUse("region", inputs, blocks)).toBe("input");
    expect(classifyUse("fetch_rows", inputs, blocks)).toBe("block");
    expect(classifyUse("fetch_rows_output", inputs, blocks)).toBe("block");
    expect(classifyUse("Not_A_Block", inputs, blocks)).toBe("neither");
  });
});
