import { afterEach, describe, expect, test, vi } from "vitest";
import * as toastModule from "@/components/ui/use-toast";

import { isWorkflowBlockNode, type AppNode } from "../nodes";
import { getElements } from "../workflowEditorUtils";
import { createDocEditApi, docDestinations } from "./docEditApi";
import { docStepChoices } from "./docStepChoices";
import type { DocGraph } from "./docGraphEdits";
import { buildDocTree } from "./buildDocTree";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { code, conditional, forLoop, settings } from "./docTestBlocks";

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
