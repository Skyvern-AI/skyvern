import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";

import type {
  WorkflowBlock,
  WorkflowSettings,
} from "../../types/workflowTypes";
import type { BlockYAML } from "../../types/workflowYamlTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements, getWorkflowBlocks } from "../workflowEditorUtils";
import {
  buildDocTree,
  countDocSteps,
  type DocBranch,
  type DocItem,
} from "./buildDocTree";

const settings = (finallyBlockLabel: string | null = null) =>
  ({
    proxyLocation: ProxyLocation.Residential,
    finallyBlockLabel,
  }) as unknown as WorkflowSettings;

type Extra = Record<string, unknown>;
const block = (
  block_type: string,
  label: string,
  next: string | null,
  extra: Extra = {},
) =>
  ({
    label,
    block_type,
    next_block_label: next,
    continue_on_failure: false,
    model: null,
    output_parameter: {
      parameter_type: "output",
      key: `${label}_output`,
      output_parameter_id: `op-${label}`,
    },
    ...extra,
  }) as unknown as WorkflowBlock;
const code = (label: string, next: string | null, extra: Extra = {}) =>
  block("code", label, next, { code: "", parameters: [], ...extra });
const forLoop = (
  label: string,
  next: string | null,
  loopBlocks: WorkflowBlock[],
  extra: Extra = {},
) =>
  block("for_loop", label, next, {
    loop_over: { key: "items" },
    loop_blocks: loopBlocks,
    loop_variable_reference: "{{ items }}",
    complete_if_empty: false,
    ...extra,
  });
type Branch = { id: string; next: string | null; when?: string; exact?: true };
const conditional = (
  label: string,
  next: string | null,
  branches: Branch[],
  extra: Extra = {},
) =>
  block("conditional", label, next, {
    branch_conditions: branches.map((b) => ({
      id: b.id,
      description: null,
      next_block_label: b.next,
      is_default: b.when === undefined,
      criteria:
        b.when === undefined
          ? null
          : {
              criteria_type: b.exact ? "jinja2_template" : "prompt",
              expression: b.when,
              description: null,
            },
    })),
    ...extra,
  });

function project(blocks: WorkflowBlock[], finallyLabel: string | null = null) {
  const { nodes, edges } = getElements(blocks, settings(finallyLabel), true);
  const tree = buildDocTree(nodes, edges, finallyLabel);
  const labelOf = new Map(
    nodes.filter(isWorkflowBlockNode).map((n) => [n.id, n.data.label]),
  );
  return { nodes, edges, tree, labelOf };
}

function countBlocks(blocks: WorkflowBlock[]): number {
  return blocks.reduce(
    (n, b) =>
      n +
      1 +
      ("loop_blocks" in b && Array.isArray(b.loop_blocks)
        ? countBlocks(b.loop_blocks as WorkflowBlock[])
        : 0),
    0,
  );
}

// Every block in the definition must appear in the document exactly once.
function placedLabels(items: DocItem[], labelOf: Map<string, string>) {
  const out: string[] = [];
  const visit = (list: DocItem[]) => {
    for (const item of list) {
      if (item.kind === "note") continue;
      if (item.kind === "unplaced") {
        out.push(...item.nodeIds.map((id) => labelOf.get(id)!));
        continue;
      }
      out.push(labelOf.get(item.nodeId)!);
      if (item.kind === "loop") visit(item.children);
      if (item.kind === "conditional")
        item.branches.forEach((b) => visit(b.children));
    }
  };
  visit(items);
  return out;
}

function expectNothingMissing(blocks: WorkflowBlock[], finallyLabel?: string) {
  const { tree, labelOf } = project(blocks, finallyLabel ?? null);
  const labels = placedLabels(tree.items, labelOf);
  expect(new Set(labels).size).toBe(labels.length);
  expect(labels.length).toBe(countBlocks(blocks));
  expect(countDocSteps(tree.items)).toBe(countBlocks(blocks));
  return { tree, labelOf };
}

const outline = (items: DocItem[], labelOf: Map<string, string>): unknown[] =>
  items.map((item) => {
    if (item.kind === "note") return `note:${item.placement}:${item.text}`;
    if (item.kind === "unplaced")
      return `unplaced:${item.nodeIds.map((id) => labelOf.get(id)).join(",")}`;
    const label = labelOf.get(item.nodeId)!;
    const tag = `${label}${item.finally ? " (finally)" : ""}${item.reachedFrom ? ` (from ${item.reachedFrom})` : ""}${item.readOnlyReason ? " (read-only)" : ""}`;
    if (item.kind === "loop") return { [tag]: outline(item.children, labelOf) };
    if (item.kind === "conditional")
      return {
        [tag]: item.branches.map((b: DocBranch) => ({
          [`${b.kind}${b.synthetic ? "*" : ""}:${b.condition}:${b.check}`]: [
            ...outline(b.children, labelOf),
            ...(b.jumpTo ? [`jump:${labelOf.get(b.jumpTo)}`] : []),
          ],
        })),
      };
    return tag;
  });

describe("buildDocTree", () => {
  test("nests loops two deep and keeps their order", () => {
    const blocks = [
      code("first", "outer"),
      forLoop("outer", "last", [
        code("in_a", "inner"),
        forLoop("inner", null, [
          code("deep_a", "deep_b"),
          code("deep_b", null),
        ]),
      ]),
      code("last", null),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(outline(tree.items, labelOf)).toEqual([
      "first",
      { outer: ["in_a", { inner: ["deep_a", "deep_b"] }] },
      "last",
    ]);
  });

  test("lists six branches in check order with Otherwise last, each with its check mode", () => {
    const blocks = [
      conditional("route", "done", [
        { id: "else", next: "e1" },
        ...[1, 2, 3, 4].map((i) => ({
          id: `b${i}`,
          next: `s${i}`,
          when: `case ${i}`,
        })),
        {
          id: "b5",
          next: "s5",
          when: "{{ total > 100 }}",
          exact: true as const,
        },
      ]),
      ...[1, 2, 3, 4, 5].map((i) => code(`s${i}`, "done")),
      code("e1", "done"),
      code("done", null),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(outline(tree.items, labelOf)).toEqual([
      {
        route: [
          { "if:case 1:ai": ["s1"] },
          { "elseIf:case 2:ai": ["s2"] },
          { "elseIf:case 3:ai": ["s3"] },
          { "elseIf:case 4:ai": ["s4"] },
          { "elseIf:{{ total > 100 }}:exact": ["s5"] },
          { "otherwise::null": ["e1"] },
        ],
      },
      "done",
    ]);
  });

  test("adds an Otherwise when the definition has none, without touching the graph", () => {
    const blocks = [
      conditional("check", "after", [
        { id: "yes", next: "act", when: "found" },
      ]),
      code("act", "after"),
      code("after", null),
    ];
    const { nodes, edges, tree, labelOf } = project(blocks);
    const before = JSON.stringify({ nodes, edges });
    buildDocTree(nodes, edges);
    expect(JSON.stringify({ nodes, edges })).toBe(before);
    const { branches } = tree.items[0] as { branches: DocBranch[] };
    expect(branches[branches.length - 1]).toMatchObject({
      kind: "otherwise",
      synthetic: true,
    });
    expect(placedLabels(tree.items, labelOf)).toHaveLength(3);
  });

  test("branches that rejoin end in a Jump and the shared step shows once after the conditional", () => {
    const blocks = [
      code("start", "decide"),
      conditional("decide", "summary", [
        { id: "failed", next: "retry", when: "a download failed" },
        { id: "expiring", next: "flag", when: "a course expires soon" },
        { id: "other", next: null },
      ]),
      code("retry", "recheck"),
      code("flag", "recheck"),
      code("recheck", "summary"),
      code("summary", null),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(outline(tree.items, labelOf)).toEqual([
      "start",
      {
        decide: [
          { "if:a download failed:ai": ["retry", "jump:recheck"] },
          { "elseIf:a course expires soon:ai": ["flag", "jump:recheck"] },
          { "otherwise::null": [] },
        ],
      },
      "recheck (from 2)",
      "summary",
    ]);
  });

  test("puts the finally step last and marks it", () => {
    const blocks = [code("cleanup", null), code("a", "b"), code("b", null)];
    const { tree, labelOf } = expectNothingMissing(blocks, "cleanup");
    expect(outline(tree.items, labelOf)).toEqual([
      "a",
      "b",
      "cleanup (finally)",
    ]);
  });

  test("a conditional at the end stays readable when a finally step follows", () => {
    const blocks = [
      code("a", "decide"),
      conditional("decide", null, [
        { id: "x", next: "s1", when: "x" },
        { id: "else", next: null },
      ]),
      code("s1", null),
      code("cleanup", null),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks, "cleanup");
    expect(outline(tree.items, labelOf)).toEqual([
      "a",
      { decide: [{ "if:x:ai": ["s1"] }, { "otherwise::null": [] }] },
      "cleanup (finally)",
    ]);
  });

  test("a branch edge into another scope makes that part read-only, and nothing is dropped", () => {
    const blocks = [
      forLoop("loop", "decide", [code("inside", "tail")]),
      conditional("decide", null, [
        { id: "go", next: "inside", when: "jump into the loop" },
        { id: "else", next: null },
      ]),
      code("tail", null),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(JSON.stringify(outline(tree.items, labelOf))).toMatch(
      /read-only|unplaced/,
    );
  });

  test("shows legacy block types as ordinary steps", () => {
    const blocks = [
      block("task", "legacy_task", "s3", {
        url: "",
        title: "",
        navigation_goal: "Go",
      }),
      block("upload_to_s3", "s3", "parse", { path: "" }),
      block("pdf_parser", "parse", "exp", { file_url: "" }),
      block("data_export", "exp", null, {}),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(outline(tree.items, labelOf)).toEqual([
      "legacy_task",
      "s3",
      "parse",
      "exp",
    ]);
  });

  test("places notes above their step and trailing notes after the last step of each sequence", () => {
    const blocks = [
      code("a", "loop", { notes: "Before a" }),
      forLoop(
        "loop",
        "decide",
        [
          code("in_a", null, {
            notes: "Before in_a",
            trailing_notes: "End of loop",
          }),
        ],
        { notes: "Before loop" },
      ),
      conditional(
        "decide",
        null,
        [
          { id: "yes", next: "y1", when: "it worked" },
          { id: "no", next: null },
        ],
        { notes: "Before decide" },
      ),
      code("y1", null, {
        notes: "Start of branch",
        trailing_notes: "End of branch",
      }),
    ];
    const { tree, labelOf } = expectNothingMissing(blocks);
    expect(outline(tree.items, labelOf)).toEqual([
      "note:above:Before a",
      "a",
      "note:above:Before loop",
      {
        loop: ["note:above:Before in_a", "in_a", "note:trailing:End of loop"],
      },
      "note:above:Before decide",
      {
        decide: [
          {
            "if:it worked:ai": [
              "note:above:Start of branch",
              "y1",
              "note:trailing:End of branch",
            ],
          },
          { "otherwise::null": [] },
        ],
      },
    ]);
  });
});

describe("doc fields round-trip through the editor graph", () => {
  const strip = (blocks: BlockYAML[]): unknown =>
    blocks.map((b) => ({
      label: b.label,
      ...("notes" in b && { notes: b.notes }),
      ...("trailing_notes" in b && { trailing_notes: b.trailing_notes }),
      ...("description" in b && { description: b.description }),
      ...("loop_blocks" in b && { loop_blocks: strip(b.loop_blocks) }),
    }));

  test("saves the fields that were set and never adds nulls", () => {
    const blocks = [
      code("plain", "noted"),
      code("noted", "loop", { notes: "Read this", description: "Adds totals" }),
      forLoop(
        "loop",
        "nulls",
        [code("inner", null, { trailing_notes: "Last" })],
        {
          notes: "Loop note",
        },
      ),
      code("nulls", null, { notes: null }),
    ];
    const { nodes, edges } = getElements(blocks, settings(), true);
    expect(strip(getWorkflowBlocks(nodes, edges))).toEqual([
      { label: "plain" },
      { label: "noted", notes: "Read this", description: "Adds totals" },
      {
        label: "loop",
        notes: "Loop note",
        loop_blocks: [{ label: "inner", trailing_notes: "Last" }],
      },
      { label: "nulls" },
    ]);
  });
});
