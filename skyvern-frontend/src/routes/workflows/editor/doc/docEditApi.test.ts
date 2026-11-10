import { afterEach, describe, expect, test, vi } from "vitest";
import * as toastModule from "@/components/ui/use-toast";

import { isWorkflowBlockNode, type AppNode } from "../nodes";
import {
  createNode,
  getElements,
  getWorkflowBlocks,
} from "../workflowEditorUtils";
import { createDocEditApi, docDestinations, TOP_SCOPE } from "./docEditApi";
import { docStepChoices } from "./docStepChoices";
import type { DocGraph } from "./docGraphEdits";
import { buildDocTree } from "./buildDocTree";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { block, code, conditional, forLoop, settings } from "./docTestBlocks";
import { docStepProblems, loopSchemaStepId } from "./docProblems";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useDocUnfinishedStore } from "./docUnfinished";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";

afterEach(() => vi.restoreAllMocks());

describe("doc edit api", () => {
  test("a refused consumer move names the moved step and the actual reference problem", () => {
    const graph = getElements(
      [code("read", "use"), code("use", null, { code: "{{ read_output }}" })],
      settings(),
      true,
    ) as DocGraph;
    const moved = graph.nodes.find((node) => node.data.label === "use")!;
    const notification = vi
      .spyOn(toastModule, "toast")
      .mockReturnValue({ id: "test", dismiss: vi.fn(), update: vi.fn() });
    let applied = graph;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });

    expect(
      api.move(moved.id, {
        scope: { parentId: null, conditionalBranchId: null },
        index: 0,
      }),
    ).toBe(false);
    expect(applied).toBe(graph);
    expect(notification).toHaveBeenCalledWith(
      expect.objectContaining({
        title: expect.stringContaining('"use"'),
        description: expect.stringContaining('"use"'),
      }),
    );
  });

  test("notes offer parameters but not block outputs, while prompts keep earlier outputs", () => {
    const graph = getElements(
      [code("read", "after"), code("after", null)],
      settings(),
      true,
    ) as DocGraph;
    const after = graph.nodes.find((node) => node.data.label === "after")!;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: () => {},
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: ["start_url", "external_output", "credential"],
      credentialKeys: new Set(["credential"]),
    });
    const keys = (inNote: boolean) =>
      api
        .references(after.id, inNote)
        .flatMap((group) => group.items.map((item) => item.key));

    expect(keys(true)).toEqual(["start_url", "external_output"]);
    expect(keys(false)).toEqual([
      "start_url",
      "external_output",
      "credential",
      "read_output",
    ]);
  });

  test("move destinations handle an unclosed Jinja condition in linear time", () => {
    const graph = getElements(
      [
        conditional("check", null, [
          { id: "b-if", next: null, when: "{{" + " ".repeat(200_000) + "x" },
          { id: "b-default", next: null },
        ]),
      ],
      settings(),
      true,
    ) as DocGraph;
    const destinations = docDestinations(
      buildDocTree(graph.nodes, graph.edges).items,
      new Map(graph.nodes.map((node) => [node.id, node])),
      () => "",
    );
    expect(destinations[1]?.label).toBe("If {{" + " ".repeat(37) + "…");
  }, 1000);

  test("moving into a loop applies the move before requesting Locate to reveal it", () => {
    const graph = getElements(
      [code("move_me", "each"), forLoop("each", null, [])],
      settings(),
      true,
    ) as DocGraph;
    const moved = graph.nodes.find((node) => node.data.label === "move_me")!;
    const loop = graph.nodes.find((node) => node.data.label === "each")!;
    let applied = graph;
    useLocateBlockStore.getState().clearLocate();
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    expect(
      api.move(moved.id, {
        scope: { parentId: loop.id, conditionalBranchId: null },
        index: 0,
      }),
    ).toBe(true);
    expect(applied.nodes.find((node) => node.id === moved.id)?.parentId).toBe(
      loop.id,
    );
    expect(useLocateBlockStore.getState().request?.nodeId).toBe(moved.id);
    useLocateBlockStore.getState().clearLocate();
  });

  test("deleting the finally step clears the finally label, so the save is not refused", () => {
    const graph = getElements(
      [code("first", "second"), code("second", null), code("cleanup", null)],
      settings("cleanup"),
      true,
    ) as DocGraph;
    const cleanup = graph.nodes.find(
      (n) => isWorkflowBlockNode(n) && n.data.label === "cleanup",
    )!;
    let applied: DocGraph | null = null;
    const api = createDocEditApi({
      nodes: graph.nodes,
      edges: graph.edges,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: cleanup.id,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    const idOf = (label: string) =>
      graph.nodes.find((n) => isWorkflowBlockNode(n) && n.data.label === label)!
        .id;
    // Focus after deleting the finally step goes to the step above it, not to the first step.
    expect(api.neighbourOf(cleanup.id)).toBe(idOf("second"));
    expect(api.neighbourOf(idOf("first"))).toBe(idOf("second"));
    expect(api.remove(cleanup.id)).toBe(true);
    const start = (applied as DocGraph | null)?.nodes.find(
      (n: AppNode) => n.type === "start" && !n.parentId,
    );
    expect(
      (start?.data as { finallyBlockLabel?: string | null }).finallyBlockLabel,
    ).toBeNull();
  });

  test("removing the selected branch selects one that remains", () => {
    const graph = getElements(
      [
        conditional("check", null, [
          { id: "b-if", next: null, when: "ready" },
          { id: "b-else-if", next: null, when: "done" },
          { id: "b-default", next: null },
        ]),
      ],
      settings(),
      true,
    ) as DocGraph;
    const conditionalNode = graph.nodes.find(
      (node) => isWorkflowBlockNode(node) && node.data.label === "check",
    )!;
    let selected = "b-if";
    let applied: DocGraph | null = null;
    const api = createDocEditApi({
      nodes: graph.nodes,
      edges: graph.edges,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: (_id, branchId) => {
        selected = branchId;
      },
      selectedBranch: () => selected,
      inputKeys: [],
      credentialKeys: new Set(),
    });

    api.removeBranch(conditionalNode.id, "b-if");

    const updated = (applied as DocGraph | null)?.nodes.find(
      (node) => node.id === conditionalNode.id,
    );
    expect(selected).not.toBe("b-if");
    expect(
      updated?.type === "conditional" &&
        updated.data.branches.map((branch) => branch.id),
    ).toContain(selected);
  });
});

describe("Doc step choices", () => {
  const offered = (agentBlockEnabled: boolean) =>
    docStepChoices({ agentBlockEnabled }).flatMap((group) =>
      group.items.map((item) => item.nodeType),
    );

  test("Agent step is offered only when its flag is on", () => {
    expect(offered(false)).not.toContain("agentStep");
    expect(offered(true)).toContain("agentStep");
  });
});

describe("building from scratch in the doc", () => {
  const editing = (graph: DocGraph, selected?: string) => {
    let current = graph;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          current = next;
        },
        update: (change) => {
          current = { ...current, nodes: change(current.nodes) };
        },
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => selected,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    return { api, latest: () => current };
  };
  // Like `editing`, but a new step really lands in the graph, as the studio's addStep does.
  const editingAdding = (graph: DocGraph) => {
    let current = graph;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          current = next;
        },
        update: (change) => {
          current = { ...current, nodes: change(current.nodes) };
        },
        addStep: (props) => {
          current = {
            ...current,
            nodes: [
              ...current.nodes,
              createNode(
                { id: "added", parentId: props.parent },
                props.nodeType,
                props.label ?? "added",
              ) as AppNode,
            ],
          };
          return "added";
        },
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    return { api, latest: () => current };
  };
  const byLabel = (graph: DocGraph, label: string) =>
    graph.nodes.find(
      (node) => isWorkflowBlockNode(node) && node.data.label === label,
    )!;

  test("an @ on the loop line picks its list, and the missing list is flagged until then", () => {
    const graph = getElements(
      [
        forLoop("books", null, [code("inner", null)], {
          loop_over: null,
          loop_variable_reference: null,
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const loop = byLabel(graph, "books");
    expect(docStepProblems(graph.nodes).get(loop.id)).toEqual([
      "This loop has no list to go through. Type @ on its line and pick a list.",
    ]);
    const { api, latest } = editing(graph);
    api.setText(loop.id, "description", "For each book in {{ list_output }}");
    const saved = byLabel(latest(), "books");
    expect(saved.data).toMatchObject({
      loopVariableReference: "{{ list_output }}",
    });
    expect(docStepProblems(latest().nodes).has(loop.id)).toBe(false);
  });

  const noSchemaList =
    "extraction_1 has no top-level list in its Data Schema, so the loop has nothing to go through. Add a list field at the top level of its Data Schema, then pick it here with @.";
  const loopAfter = (source: ReturnType<typeof block>, extra = {}) =>
    getElements(
      [
        source,
        forLoop("books", null, [code("inner", null)], {
          loop_over: null,
          ...extra,
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
  const loopProblems = (graph: DocGraph) =>
    docStepProblems(graph.nodes).get(byLabel(graph, "books").id);
  const extraction = (dataSchema: unknown = null) =>
    block("extraction", "extraction_1", "books", {
      data_extraction_goal: "List the titles",
      data_schema: dataSchema,
    });

  test.each([
    ["{{ extraction_1_output }}", null, true],
    ["{{extraction_1_output}}", null, true],
    ["extraction_1_output", null, true],
    ["{{ extraction_1 }}", null, true],
    ["extraction_1", null, true],
    ["{{ extraction_1_output.extracted_information.titles }}", null, false],
    ["{{ extraction_1_output | map(attribute='x') }}", null, false],
    ["{{ book_list }}", null, false],
  ])(
    "a loop over %s (data schema %j) is flagged only when it goes through a whole result record",
    (reference, dataSchema, flagged) => {
      const problems = loopProblems(
        loopAfter(extraction(dataSchema), {
          loop_variable_reference: reference,
        }),
      );
      if (flagged) expect(problems).toEqual([noSchemaList]);
      else expect(problems).toBeUndefined();
    },
  );

  test("a loop over an extraction's whole result says what to pick, by what its Data Schema holds", () => {
    const resultsSchema = {
      type: "object",
      properties: { results: { type: "array" } },
    };
    const over = (
      dataSchema: unknown,
      reference = "{{ extraction_1_output }}",
    ) =>
      loopAfter(extraction(dataSchema), { loop_variable_reference: reference });
    const noSchema = over(null);
    expect(loopProblems(noSchema)).toEqual([noSchemaList]);
    expect(loopSchemaStepId(byLabel(noSchema, "books"), noSchema.nodes)).toBe(
      byLabel(noSchema, "extraction_1").id,
    );
    const withList = over(resultsSchema);
    expect(loopProblems(withList)).toEqual([
      "extraction_1_output is the whole result of extraction_1, not a list. Pick one of its list fields with @.",
    ]);
    expect(loopSchemaStepId(byLabel(withList, "books"), withList.nodes)).toBe(
      null,
    );
    expect(loopProblems(over({ type: "array" }))).toEqual([
      "extraction_1_output is the whole result of extraction_1, not a list. Its Data Schema is a list, so use extraction_1_output.extracted_information.",
    ]);
    // The backend reads a nullable array schema as an array.
    expect(
      loopProblems(
        over({ type: ["array", "null"], items: { type: "object" } }),
      ),
    ).toEqual([
      "extraction_1_output is the whole result of extraction_1, not a list. Its Data Schema is a list, so use extraction_1_output.extracted_information.",
    ]);
    // A union takes its first non-null type, as the backend does: this one is an object.
    expect(
      loopProblems(
        over({ ...resultsSchema, type: ["object", "array", "null"] }),
      ),
    ).toEqual([
      "extraction_1_output is the whole result of extraction_1, not a list. Pick one of its list fields with @.",
    ]);
    const nested = over({
      type: "object",
      properties: {
        page: { type: "object", properties: { books: { type: "array" } } },
      },
    });
    // A list one level down is not offered by @, so the message says top-level.
    expect(loopProblems(nested)).toEqual([noSchemaList]);
    expect(
      loopProblems(
        over({ type: "object", properties: { items: { type: "array" } } }),
      ),
    ).toEqual([
      `Rename the list field "items" in the earlier step's data schema. A loop can't go through a field with that name.`,
    ]);
    expect(loopProblems(over("{ not json"))).toEqual([
      "extraction_1_output is the whole result of extraction_1, not a list. Pick one of its list fields with @.",
    ]);
    expect(
      loopProblems(
        over(
          resultsSchema,
          "{{ extraction_1_output.extracted_information.results }}",
        ),
      ),
    ).toBeUndefined();
  });

  const recordHint = (blockType: string, path: string) =>
    blockType === "task"
      ? /^.+ has no top-level list in its Data Schema, so the loop has nothing to go through\./
      : blockType === "navigation"
        ? /^task_1_output is the whole result of .+, not a list, and this step's result usually holds no list\. Pick a list from an earlier extraction with @\.$/
        : new RegExp(
            `^task_1_output is the whole result of .+, not a list\\. Pick the list inside it, for example task_1_output\\.${path}\\.$`,
          );

  test.each([
    ["task", {}, "extracted_information.<field>"],
    ["navigation", {}, "extracted_information.<field>"],
    ["http_request", {}, "body"],
    ["google_sheets_read", {}, "rows"],
    ["google_sheets_read", { has_header_row: false }, "values"],
    ["email_inbox", {}, "emails"],
    ["web_search", {}, "results"],
    ["code", { code: "", parameters: [] }, null],
    ["code", { code: "items = [1, 2]\nreturn items", parameters: [] }, null],
    ["file_url_parser", {}, null],
    ["text_prompt", {}, null],
    ["for_loop", { loop_over: { key: "items" }, loop_blocks: [] }, null],
  ])(
    "a loop over a whole %s result (%j) is flagged only for a step whose output is always a record",
    (blockType, extra, path) => {
      const problems = loopProblems(
        loopAfter(block(blockType, "task_1", "books", extra), {
          loop_variable_reference: "{{ task_1_output }}",
        }),
      );
      if (path)
        expect(problems).toEqual([
          expect.stringMatching(recordHint(blockType, path)),
        ]);
      else expect(problems).toBeUndefined();
    },
  );

  test("a label that itself ends in _output resolves as the label as written", () => {
    const graph = getElements(
      [
        block("extraction", "titles", "titles_output", {
          data_extraction_goal: "List the titles",
        }),
        block("extraction", "titles_output", "books", {
          data_extraction_goal: "List the titles",
        }),
        forLoop("books", null, [code("inner", null)], {
          loop_over: null,
          loop_variable_reference: "titles_output",
        }),
      ],
      settings(),
      true,
    ) as DocGraph;
    expect(loopProblems(graph)).toEqual([
      "Titles output has no top-level list in its Data Schema, so the loop has nothing to go through. Add a list field at the top level of its Data Schema, then pick it here with @.",
    ]);
  });

  const titlesSchema = {
    type: "object",
    properties: {
      titles: { type: "array", items: { type: "string" } },
      total: { type: "integer" },
    },
  };
  const loopListItems = (graph: DocGraph) => {
    const { api } = editing(graph);
    return api
      .references(byLabel(graph, "books").id, false)
      .flatMap((group) => group.items);
  };
  const savedLoop = (graph: DocGraph) =>
    getWorkflowBlocks(graph.nodes, graph.edges).find(
      (b) => b.label === "books",
    );

  test("@ on a loop's list offers an earlier extraction's array field by its plain name, and picking it saves the hand-typed path", () => {
    const graph = loopAfter(extraction(titlesSchema), {
      loop_variable_reference: null,
    });
    const offered = loopListItems(graph).find(
      (item) => item.label === "titles",
    );
    expect(offered).toBeDefined();
    expect(loopListItems(graph).map((item) => item.label)).not.toContain(
      "total",
    );

    // The menu inserts `{{ key }}` at the caret, as for any reference.
    const picked = editing(graph);
    const loopId = byLabel(graph, "books").id;
    picked.api.setText(loopId, "description", `{{ ${offered!.key} }}`);
    const typed = editing(graph);
    typed.api.setText(
      loopId,
      "description",
      "{{ extraction_1_output.extracted_information.titles }}",
    );
    expect(savedLoop(picked.latest())).toEqual(savedLoop(typed.latest()));
    expect(savedLoop(picked.latest())).toMatchObject({
      loop_variable_reference:
        "{{ extraction_1_output.extracted_information.titles }}",
    });
    expect(loopProblems(picked.latest())).toBeUndefined();
  });

  test("an extraction without an array field offers no list, and its bare result still holds Run", () => {
    const graph = loopAfter(
      extraction({
        type: "object",
        properties: { total: { type: "integer" } },
      }),
      { loop_variable_reference: "{{ extraction_1_output }}" },
    );
    expect(loopListItems(graph).map((item) => item.key)).toEqual([
      "extraction_1_output",
    ]);
    expect(loopProblems(graph)).toEqual([
      "extraction_1 has no top-level list in its Data Schema, so the loop has nothing to go through. Add a list field at the top level of its Data Schema, then pick it here with @.",
    ]);
  });

  test("a Unicode list field is offered and saves its dotted path; a name with a space or a dash is not", () => {
    const graph = loopAfter(
      extraction({
        type: "object",
        properties: {
          éléments: { type: "array" },
          "two words": { type: "array" },
          "with-dash": { type: "array" },
        },
      }),
      { loop_variable_reference: null },
    );
    expect(
      loopListItems(graph)
        .map((item) => item.label)
        .filter(Boolean),
    ).toEqual(["éléments"]);
    const offered = loopListItems(graph).find(
      (item) => item.label === "éléments",
    );
    const { api, latest } = editing(graph);
    api.setText(
      byLabel(graph, "books").id,
      "description",
      `{{ ${offered!.key} }}`,
    );
    expect(savedLoop(latest())).toMatchObject({
      loop_variable_reference:
        "{{ extraction_1_output.extracted_information.éléments }}",
    });
    expect(loopProblems(latest())).toBeUndefined();
  });

  test("a list field named like a dict method or a keyword is not offered and holds Run when typed, and a note on the loop gets no list", () => {
    const schema = {
      type: "object",
      properties: {
        items: { type: "array" },
        keys: { type: "array" },
        class: { type: "array" },
        titles: { type: "array" },
      },
    };
    const graph = loopAfter(extraction(schema), {
      loop_variable_reference: null,
    });
    expect(loopListItems(graph)).toEqual([
      {
        key: "extraction_1_output",
        hint: "Whole result, not a list",
      },
      {
        key: "extraction_1_output.extracted_information.titles",
        label: "titles",
        hint: "List in extraction_1_output",
      },
    ]);
    const loopId = byLabel(graph, "books").id;
    expect(editing(graph).api.references(loopId, true)).toEqual([]);

    const typed = (field: string) =>
      loopProblems(
        loopAfter(extraction(schema), {
          loop_variable_reference: `{{ extraction_1_output.extracted_information.${field} }}`,
        }),
      );
    expect(typed("items")).toEqual([
      `Rename the list field "items" in the earlier step's data schema. A loop can't go through a field with that name.`,
    ]);
    expect(typed("titles")).toBeUndefined();
  });

  test("a while loop or a loop read only from its loop value is not flagged", () => {
    const graph = loopAfter(extraction(), {
      loop_variable_reference: "{{ extraction_1_output }}",
    });
    Object.assign(byLabel(graph, "books").data, { loopKind: "while" });
    expect(loopProblems(graph)).toBeUndefined();
    expect(
      loopProblems(
        loopAfter(extraction(), {
          loop_over: { key: "extraction_1_output" },
          loop_variable_reference: "",
        }),
      ),
    ).toBeUndefined();
  });

  test("a Go to URL's line is its address; a note naming a site never sets it", () => {
    const graph = getElements(
      [block("goto_url", "open", null, { url: "" })],
      settings(),
      true,
    ) as DocGraph;
    const open = byLabel(graph, "open");
    expect(docStepProblems(graph.nodes).get(open.id)?.[0]).toMatch(/URL/);
    const { api, latest } = editing(graph);
    api.setText(open.id, "description", "See example.com first");
    expect(byLabel(latest(), "open").data).toMatchObject({ url: "" });
    api.setText(open.id, "url", "https://example.com");
    expect(byLabel(latest(), "open").data).toMatchObject({
      url: "https://example.com",
    });
    expect(docStepProblems(latest().nodes).has(open.id)).toBe(false);
  });

  test("the If line typed in the doc is the branch condition the save reads", () => {
    const graph = getElements(
      [
        conditional("check", null, [
          { id: "b1", next: null, when: "" },
          { id: "b2", next: null },
        ]),
      ],
      settings(),
      true,
    ) as DocGraph;
    const check = byLabel(graph, "check");
    const { api, latest } = editing(graph);
    api.setCondition(check.id, "b1", "the book is in stock");
    const tree = buildDocTree(latest().nodes, latest().edges, null);
    const item = tree.items.find((i) => i.kind === "conditional");
    expect(item?.kind === "conditional" && item.branches[0]).toMatchObject({
      condition: "the book is in stock",
      check: "ai",
    });
  });

  test("Tab moves a note under a loop into the loop, and Shift+Tab moves it back out", () => {
    const graph = getElements(
      [
        forLoop("books", "after", [code("inner", null)]),
        code("after", null, { notes: "Skip books that are sold out" }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const after = byLabel(graph, "after");
    const inner = byLabel(graph, "inner");
    const books = byLabel(graph, "books");
    const { api, latest } = editing(graph);
    expect(
      api.nestNote({ ownerNodeId: after.id, placement: "above" }, false),
    ).toEqual({ ownerNodeId: inner.id, placement: "trailing" });
    expect(
      (byLabel(latest(), "inner").data as Record<string, unknown>)
        .trailingNotes,
    ).toBe("Skip books that are sold out");
    expect(
      (byLabel(latest(), "after").data as Record<string, unknown>).notes ??
        null,
    ).toBeNull();

    const back = editing(latest());
    expect(
      back.api.nestNote({ ownerNodeId: inner.id, placement: "trailing" }, true),
    ).toEqual({ ownerNodeId: books.id, placement: "trailing" });
    expect(
      (byLabel(back.latest(), "books").data as Record<string, unknown>)
        .trailingNotes,
    ).toBe("Skip books that are sold out");
  });

  test("a Code goal can't be edited in the Doc while its build is running or queued", () => {
    const graph = getElements(
      [code("gen", null)],
      settings(),
      true,
    ) as DocGraph;
    const gen = byLabel(graph, "gen");
    const { api, latest } = editing(graph);
    const prompt = () =>
      (byLabel(latest(), "gen").data as Record<string, unknown>).prompt;
    api.setText(gen.id, "prompt", "first goal");
    expect(prompt()).toBe("first goal");
    useCopilotActionStore.setState({ generatingBlockLabel: "gen" });
    api.setText(gen.id, "prompt", "second goal");
    expect(prompt()).toBe("first goal");
    useCopilotActionStore.setState({
      generatingBlockLabel: null,
      queuedBuilds: [{ blockLabel: "gen" } as never],
    });
    api.setText(gen.id, "prompt", "third goal");
    expect(prompt()).toBe("first goal");
    useCopilotActionStore.setState({ queuedBuilds: [] });
    api.setText(gen.id, "prompt", "fourth goal");
    expect(prompt()).toBe("fourth goal");
  });

  test("Tab on a note under an empty loop picks no step type: it asks for the menu and changes nothing", () => {
    const graph = getElements(
      [forLoop("books", null, [])],
      settings(),
      true,
    ) as DocGraph;
    const books = byLabel(graph, "books");
    const note = { ownerNodeId: books.id, placement: "trailing" as const };
    const { api, latest } = editing({
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === books.id
          ? ({
              ...n,
              data: { ...n.data, trailingNotes: "Open each book page" },
            } as AppNode)
          : n,
      ),
    });
    expect(api.nestNote(note, false)).toEqual({
      pickIn: { parentId: books.id, conditionalBranchId: null },
    });
    expect(latest().nodes.map((n) => n.id)).toEqual(
      graph.nodes.map((n) => n.id),
    );
    expect(
      (byLabel(latest(), "books").data as Record<string, unknown>)
        .trailingNotes,
    ).toBe("Open each book page");
  });

  test("the step picked under an empty loop keeps the note's text as its instruction", () => {
    const graph = getElements(
      [forLoop("books", null, [])],
      settings(),
      true,
    ) as DocGraph;
    const books = byLabel(graph, "books");
    const { api, latest } = editingAdding({
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === books.id
          ? ({
              ...n,
              data: { ...n.data, trailingNotes: "Open each book page" },
            } as AppNode)
          : n,
      ),
    });
    const scope = { parentId: books.id, conditionalBranchId: null };
    const id = api.adoptNote(
      { ownerNodeId: books.id, placement: "trailing" },
      scope,
      "extraction",
    );
    const step = latest().nodes.find((n) => n.id === id)!;
    expect(step.parentId).toBe(books.id);
    expect(step.type).toBe("extraction");
    expect(step.data).toMatchObject({
      dataExtractionGoal: "Open each book page",
    });
    expect(
      (byLabel(latest(), "books").data as Record<string, unknown>)
        .trailingNotes ?? "",
    ).toBe("");
  });

  test("Enter in the middle of a step splits it into two steps of the same type", () => {
    const graph = getElements(
      [code("only", null, {})],
      settings(),
      true,
    ) as DocGraph;
    const only = byLabel(graph, "only");
    const { api, latest } = editingAdding(graph);
    const id = api.splitStep({ scope: TOP_SCOPE, index: 0 }, only.id, {
      key: "prompt",
      before: "Open the page",
      after: "and read it",
    });
    const nodes = latest().nodes;
    const added = nodes.find((n) => n.id === id)!;
    expect(added.type).toBe(only.type);
    expect(added.data).toMatchObject({ prompt: "and read it" });
    expect(nodes.find((n) => n.id === only.id)!.data).toMatchObject({
      prompt: "Open the page",
    });
  });

  test("Enter at the very start of a step adds an empty step and leaves the text with its settings in the original", () => {
    const graph = getElements(
      [code("only", null, { prompt: "Open the page" })],
      settings(),
      true,
    ) as DocGraph;
    const only = byLabel(graph, "only");
    const { api, latest } = editingAdding(graph);
    const focus = api.splitStep({ scope: TOP_SCOPE, index: 0 }, only.id, {
      key: "prompt",
      before: "",
      after: "Open the page",
    });
    expect(focus).toBe(only.id);
    const nodes = latest().nodes.filter((n) => n.type === only.type);
    expect(nodes).toHaveLength(2);
    expect(nodes.find((n) => n.id === only.id)!.data).toMatchObject({
      prompt: "Open the page",
    });
    const other = nodes.find((n) => n.id !== only.id)!;
    expect((other.data as Record<string, unknown>).prompt ?? "").not.toContain(
      "Open the page",
    );
  });

  test("a step made by Enter stays quiet until the person leaves it", () => {
    useDocUnfinishedStore.getState().clear();
    const graph = getElements(
      [code("only", null, { prompt: "Open the page" })],
      settings(),
      true,
    ) as DocGraph;
    const only = byLabel(graph, "only");
    const { api } = editingAdding(graph);
    const added = api.splitStep({ scope: TOP_SCOPE, index: 0 }, only.id, {
      key: "prompt",
      before: "Open the page",
      after: "",
    });
    expect(useDocUnfinishedStore.getState().ids.has(added!)).toBe(true);
    useDocUnfinishedStore.getState().clear();
  });

  test("/ on an empty step replaces it in one add, with the neighbours joined around the new step", () => {
    const graph = getElements(
      [
        code("before", "empty_one", { prompt: "Open the page" }),
        block("goto_url", "empty_one", "after", { url: "" }),
        code("after", null),
      ],
      settings(),
      true,
    ) as DocGraph;
    const addStep = vi.fn(() => "new_loop");
    const api = createDocEditApi({
      ...graph,
      edits: { apply: vi.fn(), update: () => {}, addStep, duplicate: () => {} },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    const empty = byLabel(graph, "empty_one");

    expect(api.isEmptyStep(empty.id)).toBe(true);
    expect(api.replaceStep(empty.id, "loop")).toBe("new_loop");

    expect(addStep).toHaveBeenCalledWith(
      expect.objectContaining({
        nodeType: "loop",
        replacing: empty.id,
        previous: byLabel(graph, "before").id,
        next: byLabel(graph, "after").id,
      }),
    );
  });

  test("an empty step whose output feeds a context input is not replaced", () => {
    const graph = getElements(
      [block("goto_url", "empty_one", null, { url: "" })],
      settings(),
      true,
    ) as DocGraph;
    const empty = byLabel(graph, "empty_one");
    useWorkflowParametersStore.setState({
      parameters: [
        {
          key: "from_step",
          parameterType: "context",
          sourceParameterKey: "empty_one_output",
        },
      ] as never,
    });
    const { api } = editing(graph);
    expect(api.isEmptyStep(empty.id)).toBe(false);
    useWorkflowParametersStore.setState({ parameters: [] });
  });

  test("turning an untouched step into another type renames it and its output for the new type", () => {
    const graph = getElements(
      [block("goto_url", "go_to_url_2", null, { url: "" })],
      settings(),
      true,
    ) as DocGraph;
    const step = byLabel(graph, "go_to_url_2");
    const { api, latest } = editing(graph);
    api.turnInto(step.id, "extraction");
    const turned = latest().nodes.find((n) => n.id === step.id)!;
    expect(turned.type).toBe("extraction");
    expect(turned.data.label).toBe("extraction_1");
  });

  test("turning a step that something refers to keeps its name", () => {
    const graph = getElements(
      [
        block("goto_url", "go_to_url_2", "after", { url: "" }),
        code("after", null, { code: "{{ go_to_url_2_output }}" }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const step = byLabel(graph, "go_to_url_2");
    const { api, latest } = editing(graph);
    api.turnInto(step.id, "extraction");
    expect(latest().nodes.find((n) => n.id === step.id)!.data.label).toBe(
      "go_to_url_2",
    );
  });

  test("a note under an empty loop stays put when the picked step has no instruction field", () => {
    const graph = getElements(
      [forLoop("books", null, [])],
      settings(),
      true,
    ) as DocGraph;
    const books = byLabel(graph, "books");
    const { api, latest } = editingAdding({
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === books.id
          ? ({
              ...n,
              data: { ...n.data, trailingNotes: "Open each book page" },
            } as AppNode)
          : n,
      ),
    });
    const id = api.adoptNote(
      { ownerNodeId: books.id, placement: "trailing" },
      { parentId: books.id, conditionalBranchId: null },
      "wait",
    );
    expect(latest().nodes.find((n) => n.id === id)?.type).toBe("wait");
    expect(
      (byLabel(latest(), "books").data as Record<string, unknown>)
        .trailingNotes,
    ).toBe("Open each book page");
  });

  test("Tab won't merge a note into one that would pass the save limit", () => {
    const big = "x".repeat(1500);
    const graph = getElements(
      [
        forLoop("books", "after", [
          code("inner", null, { trailing_notes: big }),
        ]),
        code("after", null, { notes: big }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const after = byLabel(graph, "after");
    const { api, latest } = editing(graph);
    expect(
      api.nestNote({ ownerNodeId: after.id, placement: "above" }, false),
    ).toBeNull();
    expect(
      (byLabel(latest(), "after").data as Record<string, unknown>).notes,
    ).toBe(big);
  });
  test("a recording takes the place of an empty step instead of landing after it", () => {
    const graph = getElements(
      [
        block("goto_url", "go_to_url_1", "go_to_url_2", {
          url: "https://a.test",
        }),
        block("goto_url", "go_to_url_2", "extract", { url: "" }),
        code("extract", null, { description: "keep me" }),
      ],
      settings(),
      true,
    ) as DocGraph;
    let applied = graph;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: (next) => {
          applied = next;
        },
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    const blank = graph.nodes.find((n) => n.data.label === "go_to_url_2")!;
    const place = { scope: TOP_SCOPE, index: 1 };

    expect(api.canReplaceBlankStep(blank.id)).toBe(true);
    expect(api.demonstrationInsertion({ ...place, index: 2 })).toEqual({
      after_label: "go_to_url_2",
      before_label: "extract",
    });
    expect(api.demonstrationInsertion(place, blank.id)).toEqual({
      after_label: "go_to_url_1",
      before_label: "extract",
    });
    const written = graph.nodes.find((n) => n.data.label === "extract")!;
    expect(api.canReplaceBlankStep(written.id)).toBe(false);
    expect(api.removeBlankStep(blank.id)).toBe(true);
    expect(applied.nodes.some((n) => n.data.label === "go_to_url_2")).toBe(
      false,
    );
  });
  test("an empty step another step reads the output of is not replaced", () => {
    const graph = getElements(
      [
        block("goto_url", "go_to_url_1", "extract", { url: "" }),
        code("extract", null, { code: "{{ go_to_url_1_output }}" }),
      ],
      settings(),
      true,
    ) as DocGraph;
    const api = createDocEditApi({
      ...graph,
      edits: {
        apply: () => {},
        update: () => {},
        addStep: () => null,
        duplicate: () => {},
      },
      finallyId: null,
      workflowId: "wpid_test",
      selectBranch: () => {},
      selectedBranch: () => undefined,
      inputKeys: [],
      credentialKeys: new Set(),
    });
    const blank = graph.nodes.find((n) => n.data.label === "go_to_url_1")!;

    expect(api.canReplaceBlankStep(blank.id)).toBe(false);
  });
});
