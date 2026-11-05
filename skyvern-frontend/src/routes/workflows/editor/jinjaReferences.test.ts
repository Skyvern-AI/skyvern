import type { Node } from "@xyflow/react";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";

import { AffectedBlocksNotice } from "./AffectedBlocksNotice";
import {
  type AffectedBlock,
  blockDeleteTarget,
  classifyUse,
  codeBlockUses,
  containsJinjaReference,
  getAffectedBlocks,
  inputDeleteTarget,
  removeJinjaReference,
  removeJinjaReferenceFromNodes,
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

function block(
  id: string,
  type: string,
  data: Record<string, unknown>,
  parentId?: string,
): Node {
  return { id, type, parentId, position: { x: 0, y: 0 }, data };
}

const inputDelete = (key: string) =>
  inputDeleteTarget({ key, parameterType: "workflow", dataType: "string" });

function blockDelete(nodes: Array<Node>, id: string, inputKeys: string[] = []) {
  const target = blockDeleteTarget(nodes, id, new Set(inputKeys));
  return getAffectedBlocks(
    nodes.filter((node) => !target.deletedIds.has(node.id)),
    target,
  );
}

describe("delete removal and preview", () => {
  const code = "rows = {{ region | tojson }}\nprint(region)\n";
  const steps = [{ description: "Read {{ region }}" }];
  const codeNode = block("c", "codeBlock", {
    label: "Compute",
    code,
    steps,
    prompt: "Sum rows for {{ region }} only.",
    goalBeforeEdit: { prompt: "Old {{ region }} goal" },
    parameterKeys: ["region"],
  });

  test("leaves code and steps unchanged, strips the goal, and keeps goalBeforeEdit out of the preview", () => {
    const [after] = removeJinjaReferenceFromNodes([codeNode], ["region"]);
    expect(after!.data.code).toBe(code);
    expect(after!.data.steps).toEqual(steps);
    expect(after!.data.prompt).toBe("Sum rows for only.");
    expect(after!.data.goalBeforeEdit).toEqual({ prompt: "Old goal" });

    const [affected] = getAffectedBlocks([codeNode], inputDelete("region"));
    expect(affected).toMatchObject({
      isCode: true,
      codeReadKeys: ["region"],
      keys: ["region"],
      changedTexts: ["Sum rows for only."],
    });
  });

  test("a declared input read by bare name says the code will fail, and an unread one does not", () => {
    const notice = (affected: Array<AffectedBlock>) =>
      renderToStaticMarkup(
        createElement(AffectedBlocksNotice, { affectedBlocks: affected }),
      );
    const reads = block("r", "codeBlock", {
      label: "Filter",
      code: "rows = [r for r in data if r.kind == kind]\n",
      parameterKeys: ["data", "kind"],
    });
    const unread = block("n", "codeBlock", {
      label: "Other",
      code: "# kind is unused\nrows = obj.kind\nkind_x = 1\n",
      parameterKeys: ["kind"],
    });
    const affected = getAffectedBlocks([reads, unread], inputDelete("kind"));
    expect(affected.map((b) => [b.label, b.codeReadKeys])).toEqual([
      ["Filter", ["kind"]],
      ["Other", []],
    ]);
    const html = notice(affected);
    expect(html).toContain("will fail when it runs until you edit it");
    expect(html).toContain("as a code input but does not read it");
    expect(html).not.toContain("will be updated");
    expect(html).not.toContain("may fail");
    expect(notice(affected.slice(1))).not.toContain("will fail");

    const fetch = block("f", "task", { label: "Fetch" });
    const pick = block("c", "codeBlock", {
      label: "Pick",
      code: "first = Fetch_output[0]\n",
      parameterKeys: ["Fetch_output"],
    });
    const [picked] = blockDelete([fetch, pick], "f");
    expect(picked!.codeReadKeys).toEqual(["Fetch_output"]);
    expect(notice([picked!])).toContain("will fail when it runs");
  });

  test("a credential input is never called unread, since its fields bind as bare names", () => {
    const login = block("c", "codeBlock", {
      label: "Login",
      code: "page.fill('#pw', password)\n",
      parameterKeys: ["creds"],
    });
    for (const input of [
      { key: "creds", parameterType: "credential" },
      { key: "creds", parameterType: "workflow", dataType: "credential_id" },
    ] as const) {
      const affected = getAffectedBlocks([login], inputDeleteTarget(input));
      expect(affected).toMatchObject([
        { isCode: true, codeReadKeys: [], credentialKeys: ["creds"] },
      ]);
      const html = renderToStaticMarkup(
        createElement(AffectedBlocksNotice, { affectedBlocks: affected }),
      );
      expect(html).toContain(
        "will fail if the code uses it or its credential fields",
      );
      expect(html).not.toContain("does not read");
    }
  });

  test("a reference only in a Python comment does not list the code block", () => {
    const commented = block("c", "codeBlock", {
      label: "Compute",
      code: "# was {{ region }}\nprint(1)\n",
      parameterKeys: [],
    });
    expect(getAffectedBlocks([commented], inputDelete("region"))).toEqual([]);
  });

  test("previews a changed text from the sentence of its first change", () => {
    const goal = `Open the page. Type is "{{ kind }}". Next ${"x".repeat(200)}`;
    const [affected] = getAffectedBlocks(
      [block("t", "task", { label: "Fill", navigationGoal: goal })],
      inputDelete("kind"),
    );
    const [excerpt] = affected!.changedTexts;
    expect(excerpt!.startsWith('Type is "". Next x')).toBe(true);
    expect(excerpt!.endsWith("...")).toBe(true);
    expect(excerpt!.length).toBeLessThanOrEqual(163);
  });

  test("block delete lists a block once for {{ Label.field }} and {{ Label_output }}", () => {
    const nodes = [
      block("f", "task", { label: "Fetch" }),
      block("u", "task", {
        label: "Use",
        navigationGoal: "Use {{ Fetch.rows }} and {{ Fetch_output.rows }}",
      }),
      block("c", "codeBlock", {
        label: "Pick",
        code: "a = {{ Fetch.rows }}\nb = {{ Fetch_output.rows }}\n",
        parameterKeys: [],
      }),
    ];
    const affected = blockDelete(nodes, "f");
    expect(affected.map((b) => b.label)).toEqual(["Use", "Pick"]);
    expect(affected[0]!.changedTexts).toEqual(["Use and"]);
    expect(affected[1]!.keys.sort()).toEqual(["Fetch", "Fetch_output"]);
  });

  test("a delete with no references lists nothing", () => {
    const nodes = [
      block("f", "task", { label: "Fetch" }),
      block("u", "task", { label: "Use", navigationGoal: "Plain text" }),
    ];
    expect(blockDelete(nodes, "f")).toEqual([]);
    expect(getAffectedBlocks(nodes, inputDelete("region"))).toEqual([]);
  });

  test("deleting a block named like a template's loop or set variable leaves that text alone", () => {
    const goal =
      "{% for item in items %}Name: {{ item.name }}{% endfor %}{%- set total = 1 -%}{{ total }}";
    const nodes = [
      block("i", "task", { label: "item" }),
      block("t", "task", { label: "total" }),
      block("u", "task", { label: "Use", navigationGoal: goal }),
    ];
    expect(blockDelete(nodes, "i")).toEqual([]);
    expect(blockDelete(nodes, "t")).toEqual([]);
    const [after] = removeJinjaReferenceFromNodes(nodes.slice(2), [
      "item",
      "total",
    ]);
    expect(after!.data.navigationGoal).toBe(goal);
  });

  test("an input with the deleted block's label keeps its {{ }} references and lists the block", () => {
    const nodes = [
      block("f", "task", { label: "Fetch" }),
      block("u", "task", { label: "Use", navigationGoal: "Go {{ Fetch }}" }),
    ];
    const target = blockDeleteTarget(nodes, "f", new Set(["Fetch"]));
    const [kept] = removeJinjaReferenceFromNodes([nodes[1]!], target.jinjaKeys);
    expect(kept!.data.navigationGoal).toBe("Go {{ Fetch }}");
    const affected = blockDelete(nodes, "f", ["Fetch"]);
    expect(affected).toMatchObject([
      { label: "Use", inputShadowedKeys: ["Fetch"], changedTexts: [] },
    ]);
    expect(
      renderToStaticMarkup(
        createElement(AffectedBlocksNotice, { affectedBlocks: affected }),
      ),
    ).toContain("instead of the deleted block");
    expect(blockDelete(nodes, "f").map((b) => b.label)).toEqual(["Use"]);
  });

  test("deleting a block labeled like a surviving block's output keeps that output's references", () => {
    const nodes = [
      block("a", "task", { label: "Fetch" }),
      block("b", "task", { label: "Fetch_output" }),
      block("u", "task", {
        label: "Use",
        navigationGoal: "Go {{ Fetch_output }}",
      }),
    ];
    expect(blockDelete(nodes, "b")).toEqual([]);
  });

  test("deleting a block keeps {{ Label_output }} when a surviving block or an input has that name", () => {
    const use = block("u", "task", {
      label: "Use",
      navigationGoal: "Go {{ Fetch_output }}",
    });
    const fetch = block("a", "task", { label: "Fetch" });
    const survivor = block("b", "task", { label: "Fetch_output" });
    expect(
      blockDeleteTarget([fetch, survivor, use], "a", new Set()).jinjaKeys,
    ).toEqual(["Fetch"]);
    expect(blockDelete([fetch, survivor, use], "a")).toEqual([]);
    expect(blockDelete([fetch, use], "a", ["Fetch_output"])).toEqual([]);
    expect(blockDelete([fetch, use], "a").map((b) => b.label)).toEqual(["Use"]);
  });

  test("string literals, f-string text and keyword arguments are not code reads", () => {
    const reads = (code: string) =>
      getAffectedBlocks(
        [
          block("c", "codeBlock", {
            label: "C",
            code,
            parameterKeys: ["kind"],
          }),
        ],
        inputDelete("kind"),
      )[0]!.codeReadKeys;
    for (const code of [
      'print("kind")\n',
      'print(f"{other} kind")\n',
      "x = dict(kind=1)\n",
      "kind = 1\n",
    ]) {
      expect(reads(code)).toEqual([]);
    }
    for (const code of [
      'print(f"{kind}")\n',
      'print(f"{kind:{width}}")\n',
      "print(kind)\n",
      "kind == 1\n",
    ]) {
      expect(reads(code)).toEqual(["kind"]);
    }
  });

  test("a {% %} tag in code that reads the key counts as a read, and a for target does not", () => {
    const reads = (code: string) =>
      getAffectedBlocks(
        [block("c", "codeBlock", { label: "C", code, parameterKeys: [] })],
        inputDelete("kind"),
      ).map((b) => b.codeReadKeys);
    expect(reads("{% if kind %}a = 1{% endif %}\n")).toEqual([["kind"]]);
    expect(reads("{%- if x == kind -%}a = 1{% endif %}\n")).toEqual([["kind"]]);
    expect(reads("{% for kind in rows %}a = 1{% endfor %}\n")).toEqual([]);
    expect(
      reads(
        "{% for kind in [1, 2] %}{% if kind %}print({{ kind }}){% endif %}{% endfor %}\n",
      ),
    ).toEqual([]);
    expect(reads("{% set kind = 1 %}{% if kind %}a = 1{% endif %}\n")).toEqual(
      [],
    );
  });

  test("a change deep in a long sentence stays visible in the preview", () => {
    const goal = `Type ${"word ".repeat(60)}is "{{ kind }}" now.`;
    const [affected] = getAffectedBlocks(
      [block("t", "task", { label: "Fill", navigationGoal: goal })],
      inputDelete("kind"),
    );
    const [excerpt] = affected!.changedTexts;
    expect(excerpt!.startsWith("...")).toBe(true);
    expect(excerpt).toContain('is "" now.');
  });

  test("a reference removed from the end previews its sentence", () => {
    const [affected] = getAffectedBlocks(
      [
        block("t", "task", {
          label: "Fill",
          navigationGoal: "Keep this sentence. {{ kind }}",
        }),
      ],
      inputDelete("kind"),
    );
    expect(affected!.changedTexts).toEqual(["Keep this sentence."]);
  });

  test("a loop over the deleted input has its loop value cleared", () => {
    const [affected] = getAffectedBlocks(
      [block("l", "loop", { label: "Each", loopVariableReference: "items" })],
      inputDelete("items"),
    );
    expect(affected).toMatchObject({ loopValueCleared: true, keys: ["items"] });
  });

  test("deleting a loop finds references to its children's outputs", () => {
    const nodes = [
      block("l", "loop", { label: "Each", loopVariableReference: "items" }),
      block(
        "child",
        "task",
        { label: "Inner", navigationGoal: "{{ Each }}" },
        "l",
      ),
      block("u", "task", {
        label: "After",
        navigationGoal: "Report {{ Inner_output }} now",
      }),
    ];
    const affected = blockDelete(nodes, "l");
    expect(affected.map((b) => b.label)).toEqual(["After"]);
    expect(affected[0]!.changedTexts).toEqual(["Report now"]);
  });
});
