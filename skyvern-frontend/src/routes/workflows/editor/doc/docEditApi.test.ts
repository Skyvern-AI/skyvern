import { afterEach, describe, expect, test, vi } from "vitest";
import * as toastModule from "@/components/ui/use-toast";

import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { createNode, getElements } from "../workflowEditorUtils";
import { createDocEditApi, docDestinations, TOP_SCOPE } from "./docEditApi";
import { docStepChoices } from "./docStepChoices";
import type { DocGraph } from "./docGraphEdits";
import { buildDocTree } from "./buildDocTree";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { block, code, conditional, forLoop, settings } from "./docTestBlocks";
import { docStepProblems } from "./docProblems";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";

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
      [code("first", null), code("cleanup", null)],
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
    api.remove(cleanup.id);
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
      "Pick the list to go through with @",
    ]);
    const { api, latest } = editing(graph);
    api.setText(loop.id, "description", "For each book in {{ list_output }}");
    const saved = byLabel(latest(), "books");
    expect(saved.data).toMatchObject({
      loopVariableReference: "{{ list_output }}",
    });
    expect(docStepProblems(latest().nodes).has(loop.id)).toBe(false);
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
});
