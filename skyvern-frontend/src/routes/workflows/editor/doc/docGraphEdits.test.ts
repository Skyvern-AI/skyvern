import { describe, expect, test } from "vitest";

import type { WorkflowBlock } from "../../types/workflowTypes";
import type { BlockYAML } from "../../types/workflowYamlTypes";
import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { getElements, getWorkflowBlocks } from "../workflowEditorUtils";
import { buildDocTree } from "./buildDocTree";
import {
  addBranch,
  checkLabel,
  findOutputUsers,
  hasSettingsBeyondText,
  moveBlock,
  newStepLabel,
  referencesAt,
  removeBlock,
  removeBranch,
  toNote,
  turnInto,
  renameStep,
  setStepField,
  clearLongDefaultText,
  contextInputsFedBy,
  type DocGraph,
} from "./docGraphEdits";
import { block, code, conditional, forLoop, settings } from "./docTestBlocks";

function graphOf(blocks: WorkflowBlock[], finallyLabel: string | null = null) {
  return getElements(blocks, settings(finallyLabel), true) as DocGraph;
}
const idOf = (graph: DocGraph, label: string) =>
  graph.nodes.find((n) => isWorkflowBlockNode(n) && n.data.label === label)!.id;

// What a save would send, reduced to the structure: labels, nesting and next pointers.
function shape(graph: DocGraph): unknown {
  const visit = (blocks: BlockYAML[]): unknown =>
    blocks.map((b) => ({
      label: b.label,
      next: b.next_block_label ?? null,
      ...("loop_blocks" in b && { inside: visit(b.loop_blocks) }),
      ...("branch_conditions" in b && {
        branches: b.branch_conditions.map((c) => c.next_block_label),
      }),
    }));
  return visit(getWorkflowBlocks(graph.nodes, graph.edges));
}

// Loading the saved blocks again must give the same structure: the edit made a graph Visual would make.
function expectRoundTrips(graph: DocGraph) {
  const saved = getWorkflowBlocks(graph.nodes, graph.edges);
  const reloaded = graphOf(saved as unknown as WorkflowBlock[]);
  expect(shape(reloaded)).toEqual(shape(graph));
  const tree = buildDocTree(graph.nodes, graph.edges);
  expect(JSON.stringify(tree)).not.toContain("readOnlyReason");
}

const invoices = () => [
  code("open", "invoices", { code: "x = 1" }),
  forLoop("invoices", "report", [
    code("read", "save", { code: "{{ open_output }}" }),
    code("save", null, { code: "y" }),
  ]),
  code("report", null, {
    code: "{{ invoices_output }} {{ invoices_output.total }}",
    notes: "Summarise {{ invoices_output }}",
  }),
];

describe("moveBlock", () => {
  test.each(["loopVariableReference", "loopValue"])(
    "refuses moving a loop before its plain %s source or its producer after it",
    (field) => {
      const graph = graphOf([code("fetch", "each"), forLoop("each", null, [])]);
      const loopId = idOf(graph, "each");
      graph.nodes = graph.nodes.map((node) =>
        node.id === loopId
          ? ({
              ...node,
              data: { ...node.data, [field]: "fetch_output.items" },
            } as AppNode)
          : node,
      );
      for (const [id, index] of [
        [loopId, 0],
        [idOf(graph, "fetch"), 2],
      ] as const) {
        expect(
          moveBlock(
            graph,
            id,
            { parentId: null, conditionalBranchId: null },
            index,
          ),
        ).toMatchObject({
          kind: "forward-reference",
          referrerLabels: ["each"],
        });
      }
    },
  );

  test("moves a step into a loop and round-trips through a save", () => {
    const graph = graphOf(invoices());
    const loop = idOf(graph, "invoices");
    const result = moveBlock(
      graph,
      idOf(graph, "report"),
      { parentId: loop, conditionalBranchId: null },
      1,
    );
    expect(result.kind).toBe("ok");
    if (result.kind !== "ok") return;
    expect(shape(result.graph)).toEqual([
      { label: "open", next: "invoices" },
      {
        label: "invoices",
        next: null,
        inside: [
          { label: "read", next: "report" },
          { label: "report", next: "save" },
          { label: "save", next: null },
        ],
      },
    ]);
    expectRoundTrips(result.graph);
  });

  test("moves a step out of a loop to the top of the doc", () => {
    const graph = graphOf(invoices());
    const result = moveBlock(
      graph,
      idOf(graph, "save"),
      { parentId: null, conditionalBranchId: null },
      0,
    );
    if (result.kind !== "ok") throw new Error(JSON.stringify(result));
    expect(
      (shape(result.graph) as Array<{ label: string }>).map((b) => b.label),
    ).toEqual(["save", "open", "invoices", "report"]);
    expectRoundTrips(result.graph);
  });

  test("refuses moving a step into a branch when its output is read in a sibling branch", () => {
    const graph = graphOf([
      code("make", "check", { code: "x = 1" }),
      conditional("check", null, [
        { id: "b-if", next: null, when: "ready" },
        { id: "b-default", next: "use" },
      ]),
      code("use", null, { code: "{{ make_output }}" }),
    ]);
    const result = moveBlock(
      graph,
      idOf(graph, "make"),
      { parentId: idOf(graph, "check"), conditionalBranchId: "b-if" },
      0,
    );
    expect(result).toMatchObject({
      kind: "forward-reference",
      referrerLabels: ["use"],
    });
  });

  test("refuses moving a consumer before a branch-only producer named in parameterKeys", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "make", when: "ready" },
        { id: "b-default", next: "use" },
      ]),
      code("make", null),
      code("use", null, { parameters: [{ key: "make_output" }] }),
    ]);
    const result = moveBlock(
      graph,
      idOf(graph, "use"),
      { parentId: null, conditionalBranchId: null },
      0,
    );

    expect(result).toMatchObject({
      kind: "forward-reference",
      referrerLabels: ["use"],
    });
  });

  test("refuses moving a step into one branch when a step after the merge reads its output", () => {
    const graph = graphOf([
      code("make", "check", { code: "x = 1" }),
      conditional("check", "use", [
        { id: "b-if", next: null, when: "ready" },
        { id: "b-default", next: null },
      ]),
      code("use", null, { code: "{{ make_output }}" }),
    ]);
    const result = moveBlock(
      graph,
      idOf(graph, "make"),
      { parentId: idOf(graph, "check"), conditionalBranchId: "b-if" },
      0,
    );
    expect(result).toMatchObject({
      kind: "forward-reference",
      referrerLabels: ["use"],
    });
  });

  test("moves a step into an empty Else-if branch", () => {
    const graph = graphOf([
      code("first", "check"),
      conditional("check", "last", [
        { id: "b-if", next: "inside", when: "ready" },
        { id: "b-else-if", next: null, when: "late" },
        { id: "b-default", next: null },
      ]),
      code("inside", null),
      code("last", null),
    ]);
    const check = idOf(graph, "check");
    const result = moveBlock(
      graph,
      idOf(graph, "first"),
      { parentId: check, conditionalBranchId: "b-else-if" },
      0,
    );
    if (result.kind !== "ok") throw new Error(JSON.stringify(result));
    const saved = getWorkflowBlocks(result.graph.nodes, result.graph.edges);
    const cond = saved.find((b) => b.label === "check") as BlockYAML & {
      branch_conditions: Array<{ id: string; next_block_label: string | null }>;
    };
    expect(
      cond.branch_conditions.find((b) => b.id === "b-else-if")
        ?.next_block_label,
    ).toBe("first");
    const moved = result.graph.nodes.find((n) => n.id === idOf(graph, "first"));
    // The If branch is the one showing on the canvas, so the moved step is hidden there.
    expect(moved?.hidden).toBe(true);
    expectRoundTrips(result.graph);
  });

  test("moving the If step into Otherwise leaves If going to the merge step", () => {
    const graph = graphOf([
      code("a", "cond"),
      conditional("cond", "b", [
        { id: "b-if", next: "in1", when: "x" },
        { id: "b-default", next: null },
      ]),
      code("in1", null),
      code("b", null),
    ]);
    const result = moveBlock(
      graph,
      idOf(graph, "in1"),
      { parentId: idOf(graph, "cond"), conditionalBranchId: "b-default" },
      0,
    );
    if (result.kind !== "ok") throw new Error(JSON.stringify(result));
    const branches = (graph: DocGraph) =>
      (
        getWorkflowBlocks(graph.nodes, graph.edges).find(
          (b) => b.label === "cond",
        ) as BlockYAML & {
          branch_conditions: Array<{ next_block_label: string | null }>;
        }
      ).branch_conditions.map((c) => c.next_block_label);
    expect(branches(graph)).toEqual(["in1", "b"]);
    expect(branches(result.graph)).toEqual(["b", "in1"]);
    const reloaded = graphOf(
      getWorkflowBlocks(
        result.graph.nodes,
        result.graph.edges,
      ) as unknown as WorkflowBlock[],
    );
    expect(branches(reloaded)).toEqual(["b", "in1"]);
  });

  test("refuses to move a loop inside itself and keeps the finally step last", () => {
    const graph = graphOf(
      [
        code("open", "invoices"),
        forLoop("invoices", "report", [code("save", null)]),
        code("report", null),
      ],
      "report",
    );
    const loop = idOf(graph, "invoices");
    expect(
      moveBlock(graph, loop, { parentId: loop, conditionalBranchId: null }, 0)
        .kind,
    ).toBe("refused");
    expect(
      moveBlock(
        graph,
        idOf(graph, "report"),
        { parentId: null, conditionalBranchId: null },
        0,
        idOf(graph, "report"),
      ).kind,
    ).toBe("refused");
    const result = moveBlock(
      graph,
      idOf(graph, "open"),
      { parentId: null, conditionalBranchId: null },
      5,
      idOf(graph, "report"),
    );
    if (result.kind !== "ok") throw new Error(JSON.stringify(result));
    expect(
      (shape(result.graph) as Array<{ label: string }>).map((b) => b.label),
    ).toEqual(["invoices", "open", "report"]);
  });

  test("refuses a move that would run a step before the step whose output it uses", () => {
    const graph = graphOf(invoices());
    const toTop = moveBlock(
      graph,
      idOf(graph, "report"),
      { parentId: null, conditionalBranchId: null },
      0,
    );
    expect(toTop).toMatchObject({
      kind: "forward-reference",
      referrerLabels: ["report"],
    });
    // Across scopes: "read" uses open's output, so "open" can't move into the loop after it.
    const intoLoop = moveBlock(
      graph,
      idOf(graph, "open"),
      { parentId: idOf(graph, "invoices"), conditionalBranchId: null },
      1,
    );
    expect(intoLoop).toMatchObject({
      kind: "forward-reference",
      referrerLabels: ["read"],
    });
  });
});

describe("references", () => {
  test("deleting a container counts uses of its inner steps' outputs and context inputs they feed", () => {
    const graph = graphOf([
      forLoop("each", "report", [code("inner", null)]),
      code("report", null, { code: "{{ inner_output }}" }),
    ]);
    const loop = idOf(graph, "each");
    expect(
      findOutputUsers(graph.nodes, loop).map(
        (n) => isWorkflowBlockNode(n) && n.data.label,
      ),
    ).toEqual(["report"]);
    const parameters = [
      {
        key: "total",
        parameterType: "context",
        sourceParameterKey: "inner_output",
      },
      { key: "url", parameterType: "workflow" },
    ];
    expect(contextInputsFedBy(graph.nodes, parameters, loop)).toEqual([
      "total",
    ]);
    expect(
      contextInputsFedBy(graph.nodes, parameters, idOf(graph, "report")),
    ).toEqual([]);
  });

  test("finds the steps and notes that use a step's output", () => {
    const graph = graphOf(invoices());
    const users = findOutputUsers(graph.nodes, idOf(graph, "invoices"));
    expect(
      users.map((n) => (n as AppNode & { data: { label: string } }).data.label),
    ).toEqual(["report"]);
    const notesOnly = graphOf([
      code("a", "b"),
      code("b", null, { notes: "Uses {{ a_output }}" }),
    ]);
    expect(findOutputUsers(notesOnly.nodes, idOf(notesOnly, "a"))).toHaveLength(
      1,
    );
    // The save refuses a parameter_keys entry for an output nobody saves, so a plain name counts too.
    const plainKey = graphOf([
      code("a", "b"),
      code("b", null, { parameters: [{ key: "a_output" }] }),
    ]);
    expect(findOutputUsers(plainKey.nodes, idOf(plainKey, "a"))).toHaveLength(
      1,
    );
  });

  test("rename rewrites code and notes and counts each reference", () => {
    const graph = graphOf(invoices());
    const { nodes, rewritten } = renameStep(
      graph.nodes,
      idOf(graph, "invoices"),
      "open_invoices",
    );
    expect(rewritten).toBe(3);
    const report = nodes.find((n) => n.id === idOf(graph, "report"))!;
    expect(report.data).toMatchObject({
      code: "{{ open_invoices_output }} {{ open_invoices_output.total }}",
      notes: "Summarise {{ open_invoices_output }}",
    });
    expect(checkLabel(nodes, idOf(graph, "open"), "open_invoices")).toMatch(
      /already/,
    );
    expect(
      checkLabel(nodes, idOf(graph, "open"), "invoice", ["invoice_output"]),
    ).toMatch(/conflicts with an existing workflow parameter/);
    expect(checkLabel(nodes, idOf(graph, "open"), "2nd")).not.toBeNull();
  });

  test.each(["", ".items", "[0].items"])(
    "renaming a producer updates the loop source prefix %s",
    (suffix) => {
      const graph = graphOf([
        code("read", "items"),
        forLoop("items", null, [], {
          loop_over: { key: `read_output${suffix}` },
          loop_variable_reference: `read_output${suffix}`,
        }),
      ]);
      const { nodes } = renameStep(graph.nodes, idOf(graph, "read"), "fetch");
      const loop = nodes.find((node) => node.id === idOf(graph, "items"))!;

      expect(loop.data).toMatchObject({
        loopValue: `fetch_output${suffix}`,
        loopVariableReference: `fetch_output${suffix}`,
      });
      expect(
        getWorkflowBlocks(nodes, graph.edges).find(
          (block) => block.label === "items",
        ),
      ).toMatchObject({ loop_over_parameter_key: `fetch_output${suffix}` });
    },
  );

  test("a new step is named from its type, a valid name no other step uses", () => {
    const { nodes } = graphOf([code("go_to_url_1", null), code("open", null)]);
    const label = newStepLabel(nodes, "Go to URL Block");
    expect(label).toBe("go_to_url_2");
    expect(checkLabel(nodes, "new", label)).toBeNull();
    expect(newStepLabel(nodes, "Extraction Block")).toBe("extraction_1");
    expect(
      newStepLabel([], "Email Inbox Block", ["email_inbox_1_output"]),
    ).toBe("email_inbox_2");
  });
});

describe("branches", () => {
  test("adds an Else-if before Otherwise and removes it with its steps", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "inside", when: "ready" },
        { id: "b-default", next: null },
      ]),
      forLoop("inside", null, [code("deep", null)]),
    ]);
    const check = idOf(graph, "check");
    const added = addBranch(graph, check, {
      id: "b-new",
      criteria: null,
      next_block_label: null,
      description: null,
      is_default: false,
    })!;
    const cond = added.nodes.find((n) => n.id === check)!;
    expect(
      (cond.data as { branches: Array<{ id: string }> }).branches.map(
        (b) => b.id,
      ),
    ).toEqual(["b-if", "b-new", "b-default"]);
    const removed = removeBranch(added, check, "b-if")!;
    const labels = removed.nodes
      .filter(isWorkflowBlockNode)
      .map((n) => n.data.label);
    expect(labels).toEqual(["check"]);
    expect(removeBranch(removed, check, "b-new")).toBeNull();
  });

  test("removing a branch clears output keys of its deleted steps", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "inside", when: "ready" },
        { id: "b-else-if", next: null, when: "later" },
        { id: "b-default", next: "after" },
      ]),
      code("inside", null),
      code("after", null, { parameters: [{ key: "inside_output" }] }),
    ]);
    const removed = removeBranch(graph, idOf(graph, "check"), "b-if")!;
    const after = removed.nodes.find(
      (node) => isWorkflowBlockNode(node) && node.data.label === "after",
    )!;

    expect((after.data as { parameterKeys: string[] }).parameterKeys).toEqual(
      [],
    );
  });
});

describe("delete and turn into", () => {
  test("deleting clears output metadata without editing code", () => {
    const initial = graphOf([...invoices(), forLoop("consumer", null, [])]);
    const reportId = idOf(initial, "report");
    const consumerId = idOf(initial, "consumer");
    const graph: DocGraph = {
      ...initial,
      nodes: initial.nodes.map((node) =>
        node.id === reportId
          ? ({
              ...node,
              data: {
                ...node.data,
                code: "{{ invoices_output }} {{ read_output }}",
                parameterKeys: ["invoices_output", "read_output"],
              },
            } as AppNode)
          : node.id === consumerId
            ? ({
                ...node,
                data: {
                  ...node.data,
                  loopVariableReference: "read_output",
                  loopValue: "invoices_output",
                },
              } as AppNode)
            : node,
      ),
    };
    const next = removeBlock(graph, idOf(graph, "invoices"))!;
    expect(
      (shape(next) as Array<{ label: string }>).map((b) => b.label),
    ).toEqual(["open", "report", "consumer"]);
    const report = next.nodes.find((n) => n.id === reportId)!;
    expect((report.data as { code: string }).code).toBe(
      "{{ invoices_output }} {{ read_output }}",
    );
    expect((report.data as { parameterKeys: string[] }).parameterKeys).toEqual(
      [],
    );
    const consumer = next.nodes.find((n) => n.id === consumerId)!;
    expect(consumer.data).toMatchObject({
      loopVariableReference: "",
      loopValue: "",
    });
    expectRoundTrips(next);
  });

  test("deleting the only step in a branch leaves the branch empty but present", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "inside", when: "ready" },
        { id: "b-default", next: null },
      ]),
      code("inside", null),
    ]);
    const next = removeBlock(graph, idOf(graph, "inside"))!;
    const tree = buildDocTree(next.nodes, next.edges);
    const item = tree.items[0]!;
    expect(
      item.kind === "conditional" &&
        item.branches.map((b) => b.children.length),
    ).toEqual([0, 0]);
    expect(JSON.stringify(tree)).not.toContain("readOnlyReason");
  });

  test("turn into keeps the name, the notes and the branch", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "inside", when: "ready" },
        { id: "b-default", next: null },
      ]),
      code("inside", null, { notes: "careful" }),
    ]);
    const next = turnInto(graph, idOf(graph, "inside"), "extraction")!;
    const node = next.nodes.find((n) => n.id === idOf(graph, "inside"))!;
    expect(node.type).toBe("extraction");
    expect(node.data).toMatchObject({
      label: "inside",
      notes: "careful",
      conditionalBranchId: "b-if",
    });
    expectRoundTrips(next);
  });
});

test("Turn into keeps the step's main text", () => {
  const graph = graphOf([
    block("text_prompt", "ask", null, { prompt: "Summarise the page" }),
  ]);
  const next = turnInto(graph, idOf(graph, "ask"), "extraction")!;
  expect(
    next.nodes.find((n) => n.id === idOf(graph, "ask"))!.data,
  ).toMatchObject({ dataExtractionGoal: "Summarise the page" });
});

describe("note text", () => {
  test("a note emptied down to a line break is stored as no note", () => {
    const graph = graphOf([code("a", null, { notes: "Start here" })]);
    const nodes = setStepField(graph.nodes, idOf(graph, "a"), "notes", "\n");
    const a = nodes.find((n) => n.id === idOf(graph, "a"))!;
    expect((a.data as { notes: string | null }).notes).toBeNull();
    expect(getWorkflowBlocks(nodes, graph.edges)[0]).not.toHaveProperty(
      "notes",
    );
  });
});

describe("new step text", () => {
  test("a long default prompt is cleared, a short or missing one is kept", () => {
    const long = "x".repeat(80);
    const graph = graphOf([code("a", "b"), code("b", null)]);
    const withGoal = graph.nodes.map((n) =>
      n.id === idOf(graph, "a")
        ? ({
            ...n,
            type: "login",
            data: { ...n.data, navigationGoal: long },
          } as AppNode)
        : n,
    );
    const cleared = clearLongDefaultText(withGoal, idOf(graph, "a"));
    const a = cleared.find((n) => n.id === idOf(graph, "a"))!;
    expect((a.data as { navigationGoal: string }).navigationGoal).toBe("");
    expect(clearLongDefaultText(graph.nodes, idOf(graph, "b"))).toBe(
      graph.nodes,
    );
  });
});

describe("back to plain text", () => {
  test("a step's text joins the note above the next step and its settings go", () => {
    const graph = graphOf([
      code("first", "middle"),
      code("middle", "last", { code: "print(1)", notes: "before" }),
      code("last", null, { notes: "about last" }),
    ]);
    const middle = graph.nodes.find((n) => n.id === idOf(graph, "middle"))!;
    // The main text of a code step is what the registry calls its first prose field, or its description.
    const result = toNote(graph, middle.id)!;
    expect(result.note).toEqual({
      ownerNodeId: idOf(graph, "last"),
      placement: "above",
    });
    expect(
      (shape(result.graph) as Array<{ label: string }>).map((b) => b.label),
    ).toEqual(["first", "last"]);
    const last = result.graph.nodes.find((n) => n.id === idOf(graph, "last"))!;
    expect((last.data as { notes: string }).notes).toMatch(
      /^before\n[\s\S]*about last$/,
    );
    expectRoundTrips(result.graph);
  });

  test("the last step's text becomes the trailing note of the one before", () => {
    const graph = graphOf([
      code("first", "last"),
      code("last", null, { description: "Send the report" }),
    ]);
    const result = toNote(graph, idOf(graph, "last"))!;
    expect(result.note).toEqual({
      ownerNodeId: idOf(graph, "first"),
      placement: "trailing",
    });
    const first = result.graph.nodes.find(
      (n) => n.id === idOf(graph, "first"),
    )!;
    expect((first.data as { trailingNotes: string }).trailingNotes).toContain(
      "Send the report",
    );
  });

  test("a step with settings beyond its text is not offered as plain text", () => {
    const graph = graphOf([
      code("configured", "plain", { code: "print(1)" }),
      code("plain", null, { description: "Send the report" }),
    ]);
    const node = (label: string) =>
      graph.nodes.find((n) => n.id === idOf(graph, label))!;
    expect(hasSettingsBeyondText(node("configured"))).toBe(true);
    expect(hasSettingsBeyondText(node("plain"))).toBe(false);
  });
});

describe("@ references", () => {
  test("inside a loop the loop item comes first, then inputs, then earlier outputs only", () => {
    const graph = graphOf(invoices());
    const groups = referencesAt(graph, idOf(graph, "save"), ["start_url"]);
    expect(groups.map((g) => g.name)).toEqual([
      "This loop",
      "Inputs",
      "Earlier steps",
    ]);
    expect(groups[0]!.items[0]!.key).toBe("current_value");
    // The loop holding the step isn't done yet, and "report" runs later.
    expect(groups[2]!.items.map((i) => i.key)).toEqual([
      "open_output",
      "read_output",
    ]);
  });

  test("a step after the merge doesn't offer outputs only one branch makes", () => {
    const graph = graphOf([
      conditional("check", "after", [
        { id: "b-if", next: "yes", when: "ready" },
        { id: "b-default", next: "no" },
      ]),
      code("yes", null),
      code("no", null),
      code("after", null),
    ]);
    const keys = referencesAt(graph, idOf(graph, "after"), []).flatMap((g) =>
      g.items.map((i) => i.key),
    );
    expect(keys).toEqual(["check_output"]);
  });

  test("a step in one branch doesn't offer a step from another branch", () => {
    const graph = graphOf([
      conditional("check", null, [
        { id: "b-if", next: "yes", when: "ready" },
        { id: "b-default", next: "no" },
      ]),
      code("yes", null),
      code("no", null),
    ]);
    const groups = referencesAt(graph, idOf(graph, "no"), []);
    expect(groups.flatMap((g) => g.items.map((i) => i.key))).toEqual([]);
  });
});
