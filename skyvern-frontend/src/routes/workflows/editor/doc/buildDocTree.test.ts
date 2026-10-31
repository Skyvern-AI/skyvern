import { describe, expect, test } from "vitest";

import type { WorkflowBlock } from "../../types/workflowTypes";
import type { BlockYAML } from "../../types/workflowYamlTypes";
import { isWorkflowBlockNode } from "../nodes";
import { getElements, getWorkflowBlocks } from "../workflowEditorUtils";
import {
  buildDocTree,
  countDocSteps,
  getDocNotesByNodeId,
  type DocBranch,
  type DocItem,
} from "./buildDocTree";
import { block, code, conditional, forLoop, settings } from "./docTestBlocks";

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
  test("gives each note to its step and to the step above it in a six-step sequence", () => {
    const blocks = Array.from({ length: 6 }, (_, index) =>
      code(`step_${index + 1}`, index === 5 ? null : `step_${index + 2}`, {
        notes: `Note ${index + 1}`,
      }),
    );
    const { nodes, edges } = getElements(blocks, settings(), true);

    const notesByNodeId = getDocNotesByNodeId(nodes, edges);
    const nodeIdByLabel = new Map(
      nodes
        .filter(isWorkflowBlockNode)
        .map((node) => [node.data.label, node.id]),
    );

    expect(notesByNodeId).toEqual(
      new Map(
        Array.from({ length: 6 }, (_, index) => [
          nodeIdByLabel.get(`step_${index + 1}`),
          index === 5
            ? [`Note ${index + 1}`]
            : [`Note ${index + 1}`, `Note ${index + 2}`],
        ]),
      ),
    );
  });

  test("lists a step's notes in the order the run reads them: own, next step's, trailing", () => {
    const blocks = [
      code("first", "second", {
        notes: "Own note.",
        trailing_notes: "Trailing note.",
      }),
      code("second", null, { notes: "Next step's note." }),
    ];
    const { nodes, edges } = getElements(blocks, settings(), true);
    const first = nodes.find(
      (node) => isWorkflowBlockNode(node) && node.data.label === "first",
    )!;

    expect(getDocNotesByNodeId(nodes, edges).get(first.id)).toEqual([
      "Own note.",
      "Next step's note.",
      "Trailing note.",
    ]);
  });

  test("omits a step that reads no note", () => {
    const blocks = [
      code("noted", "plain", { notes: "Visible only on noted." }),
      code("plain", null),
    ];
    const { nodes, edges } = getElements(blocks, settings(), true);
    const nodeIdByLabel = new Map(
      nodes
        .filter(isWorkflowBlockNode)
        .map((node) => [node.data.label, node.id]),
    );

    const notesByNodeId = getDocNotesByNodeId(nodes, edges);

    expect(notesByNodeId.get(nodeIdByLabel.get("noted")!)).toEqual([
      "Visible only on noted.",
    ]);
    expect(notesByNodeId.has(nodeIdByLabel.get("plain")!)).toBe(false);
  });

  test("keeps the finally block's note on the finally block, as the run does", () => {
    const blocks = [
      code("work", "cleanup"),
      code("cleanup", null, { notes: "Log out." }),
    ];
    const { nodes, edges } = getElements(blocks, settings("cleanup"), true);
    const nodeIdByLabel = new Map(
      nodes
        .filter(isWorkflowBlockNode)
        .map((node) => [node.data.label, node.id]),
    );

    const notesByNodeId = getDocNotesByNodeId(nodes, edges, "cleanup");

    expect(notesByNodeId.get(nodeIdByLabel.get("cleanup")!)).toEqual([
      "Log out.",
    ]);
    expect(notesByNodeId.has(nodeIdByLabel.get("work")!)).toBe(false);
  });

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

  test("an unplaced step keeps the note markers for its own notes", () => {
    const blocks = [
      forLoop("loop", "decide", [code("inside", "tail")]),
      conditional("decide", null, [
        { id: "go", next: "inside", when: "jump into the loop" },
        { id: "else", next: null },
      ]),
      { ...code("tail", null), notes: "after the loop", trailing_notes: "end" },
    ];
    const { nodes, edges, tree } = project(blocks);
    const unplaced = tree.items.flatMap((item) =>
      item.kind === "unplaced" ? item.nodeIds : [],
    );
    const markers = getDocNotesByNodeId(nodes, edges);
    const tail = nodes.find(
      (n) => isWorkflowBlockNode(n) && n.data.label === "tail",
    )!;

    expect(unplaced).toContain(tail.id);
    expect(markers.get(tail.id)).toEqual(["after the loop", "end"]);
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
