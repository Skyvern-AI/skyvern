import { afterEach, describe, expect, test, vi } from "vitest";
import * as toastModule from "@/components/ui/use-toast";

import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { createNode, getElements } from "../workflowEditorUtils";
import { createDocEditApi, docDestinations } from "./docEditApi";
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

  test("Tab on a note under an empty loop becomes the loop's first Browser Task, with the note's text as its goal", () => {
    const graph = getElements(
      [forLoop("books", null, [])],
      settings(),
      true,
    ) as DocGraph;
    const books = byLabel(graph, "books");
    let current: DocGraph = {
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === books.id
          ? ({
              ...n,
              data: { ...n.data, trailingNotes: "Open each book page" },
            } as AppNode)
          : n,
      ),
    };
    const calls: string[] = [];
    const api = createDocEditApi({
      ...current,
      edits: {
        apply: (next) => {
          current = next;
        },
        update: (change) => {
          calls.push("update");
          current = { ...current, nodes: change(current.nodes) };
        },
        addStep: (props) => {
          calls.push("addStep");
          current = {
            ...current,
            nodes: [
              ...current.nodes,
              createNode(
                { id: "first", parentId: props.parent },
                "navigation",
                props.label ?? "first",
              ) as AppNode,
            ],
          };
          return "first";
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
    expect(
      api.nestNote({ ownerNodeId: books.id, placement: "trailing" }, false),
    ).toEqual({ stepId: "first" });
    const step = current.nodes.find((n) => n.id === "first")!;
    // The step and the note's move both go through the debounced path, so history folds them into one entry.
    expect(calls[0]).toBe("addStep");
    expect(calls.slice(1).every((c) => c === "update")).toBe(true);
    expect(step.parentId).toBe(books.id);
    expect(step.data).toMatchObject({ navigationGoal: "Open each book page" });
    expect(
      (byLabel(current, "books").data as Record<string, unknown>)
        .trailingNotes ?? "",
    ).toBe("");
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
