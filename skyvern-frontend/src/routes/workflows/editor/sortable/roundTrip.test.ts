import type { Edge } from "@xyflow/react";
import { describe, expect, test } from "vitest";

import { ProxyLocation } from "@/api/types";

import type { AppNode } from "../nodes";
import {
  getElements,
  getWorkflowBlocks,
  getWorkflowSettings,
} from "../workflowEditorUtils";
import {
  type CodeBlock,
  type OutputParameter,
  type WorkflowBlock,
  type WorkflowSettings,
} from "../../types/workflowTypes";
import type { CodeBlockYAML } from "../../types/workflowYamlTypes";

import { rewireBlockDropInScope } from "./rewire";
import { TOP_LEVEL_SCOPE } from "./scope";

/**
 * M1 round-trip regression: mirrors the full reorder → save → reload path
 * that ships in FlowRenderer. The goal is to catch regressions anywhere
 * along the chain — SortableContext ordering, edge rewire, getWorkflowBlocks
 * serialization, or getElements edge reconstruction — without standing up
 * the full React/react-flow renderer.
 *
 * Pipeline under test (see AC on SKY-9056):
 *   SortableContext → edge rewire (rewire.ts) → constructSaveData
 *   (FlowRenderer.tsx:536) → YAML via getWorkflowBlocks
 *   (workflowEditorUtils.ts:2806) → reload → edge reconstruction
 *   via getElements (workflowEditorUtils.ts:1523).
 *
 * We also assert the invariants enforced by backend `_build_loop_graph`
 * (skyvern/forge/sdk/workflow/models/block.py:1834) so the produced shape
 * cannot drift into something the backend rejects: unique labels, every
 * `next_block_label` resolves, exactly one root, no cycles.
 */

const DEFAULT_SETTINGS: WorkflowSettings = {
  proxyLocation: ProxyLocation.Residential,
  webhookCallbackUrl: null,
  persistBrowserSession: false,
  browserProfileId: null,
  browserProfileKey: null,
  model: null,
  maxScreenshotScrolls: null,
  maxElapsedTimeMinutes: null,
  extraHttpHeaders: null,
  cdpConnectHeaders: null,
  runWith: "code",
  codeVersion: 2,
  scriptCacheKey: null,
  aiFallback: true,
  runSequentially: false,
  sequentialKey: null,
  finallyBlockLabel: null,
  workflowSystemPrompt: null,
  errorCodeMapping: null,
};

function makeOutputParameter(label: string): OutputParameter {
  return {
    parameter_type: "output",
    key: `${label}_output`,
    description: null,
    output_parameter_id: `op-${label}`,
    workflow_id: "wf-fixture",
    created_at: "2026-04-20T00:00:00Z",
    modified_at: "2026-04-20T00:00:00Z",
    deleted_at: null,
  };
}

function makeCodeBlock(
  label: string,
  nextBlockLabel: string | null,
): CodeBlock {
  return {
    label,
    block_type: "code",
    continue_on_failure: false,
    model: null,
    next_block_label: nextBlockLabel,
    output_parameter: makeOutputParameter(label),
    code: `# ${label}`,
    parameters: [],
  };
}

/** Flat 5-block fixture: B1 → B2 → B3 → B4 → B5. */
function buildFiveBlockFixture(): Array<WorkflowBlock> {
  return [
    makeCodeBlock("B1", "B2"),
    makeCodeBlock("B2", "B3"),
    makeCodeBlock("B3", "B4"),
    makeCodeBlock("B4", "B5"),
    makeCodeBlock("B5", null),
  ];
}

function findNodeIdByLabel(nodes: Array<AppNode>, label: string): string {
  const match = nodes.find(
    (node) =>
      node.type !== "start" &&
      node.type !== "nodeAdder" &&
      "data" in node &&
      node.data &&
      typeof node.data === "object" &&
      "label" in node.data &&
      (node.data as { label?: unknown }).label === label,
  );
  if (!match) {
    throw new Error(`fixture missing node for label ${label}`);
  }
  return match.id;
}

function chainFromSavedBlocks(
  blocks: Array<{ label: string; next_block_label?: string | null }>,
): Array<string> {
  if (blocks.length === 0) return [];
  const byLabel = new Map<string, (typeof blocks)[number]>();
  for (const block of blocks) byLabel.set(block.label, block);

  // Root: a block with no incoming next_block_label from any other block.
  const referenced = new Set<string>();
  for (const block of blocks) {
    if (block.next_block_label) referenced.add(block.next_block_label);
  }
  const roots = blocks
    .map((b) => b.label)
    .filter((label) => !referenced.has(label));
  if (roots.length !== 1) {
    throw new Error(
      `expected exactly one root block, found ${roots.length}: ${roots.join(", ")}`,
    );
  }

  const chain: Array<string> = [];
  const visited = new Set<string>();
  let cursor: string | null = roots[0] ?? null;
  while (cursor !== null) {
    if (visited.has(cursor)) {
      throw new Error(`cycle detected at ${cursor}`);
    }
    visited.add(cursor);
    chain.push(cursor);
    const block = byLabel.get(cursor);
    cursor = block?.next_block_label ?? null;
  }
  return chain;
}

/**
 * Mirror of `_build_loop_graph`'s safety checks on the frontend side so we
 * can assert the saved YAML is shaped in a way the backend will accept.
 * Duplicates, dangling targets, missing root, and cycles all throw — and
 * each throw reproduces the backend's exact failure mode for that YAML.
 */
function assertBackendBuildLoopGraphAccepts(
  blocks: Array<{ label: string; next_block_label?: string | null }>,
): void {
  const labels = new Set<string>();
  for (const block of blocks) {
    if (labels.has(block.label)) {
      throw new Error(`duplicate block label: ${block.label}`);
    }
    labels.add(block.label);
  }
  for (const block of blocks) {
    const next = block.next_block_label;
    if (next && !labels.has(next)) {
      throw new Error(
        `block ${block.label} references unknown next_block_label ${next}`,
      );
    }
  }
  // `chainFromSavedBlocks` enforces the single-root and acyclic invariants.
  const chain = chainFromSavedBlocks(blocks);
  if (chain.length !== blocks.length) {
    throw new Error(
      `chain walk covered ${chain.length} of ${blocks.length} blocks — disconnected graph`,
    );
  }
}

function codeYamlToWorkflowBlock(yaml: CodeBlockYAML): CodeBlock {
  // getElements expects WorkflowBlock shapes. The YAML form omits the
  // output_parameter (that lives on the workflow record) and carries
  // parameter_keys instead of full parameters. The reload simulation below
  // re-hydrates exactly the fields getElements reads.
  return {
    label: yaml.label,
    block_type: "code",
    continue_on_failure: yaml.continue_on_failure ?? false,
    next_loop_on_failure: yaml.next_loop_on_failure,
    model: null,
    next_block_label: yaml.next_block_label ?? null,
    output_parameter: makeOutputParameter(yaml.label),
    code: yaml.code,
    parameters: [],
  };
}

function reloadFromSavedYaml(
  saved: Array<{ label: string; block_type: string }>,
): { nodes: Array<AppNode>; edges: Array<Edge> } {
  const blocks = saved.map((block) => {
    if (block.block_type !== "code") {
      throw new Error(`fixture only uses code blocks, got ${block.block_type}`);
    }
    return codeYamlToWorkflowBlock(block as CodeBlockYAML);
  });
  return getElements(blocks, DEFAULT_SETTINGS, true);
}

describe("round-trip reorder → save → reload (M1 top-level)", () => {
  test("drag B3 above B1 persists as B3 → B1 → B2 → B4 → B5 chain", () => {
    // 1. Load the workflow: YAML-like blocks → nodes + edges via getElements.
    const initialBlocks = buildFiveBlockFixture();
    const { nodes, edges } = getElements(initialBlocks, DEFAULT_SETTINGS, true);

    const initialSaved = getWorkflowBlocks(nodes, edges);
    expect(initialSaved.map((b) => b.label)).toEqual([
      "B1",
      "B2",
      "B3",
      "B4",
      "B5",
    ]);
    expect(chainFromSavedBlocks(initialSaved)).toEqual([
      "B1",
      "B2",
      "B3",
      "B4",
      "B5",
    ]);

    const b1Id = findNodeIdByLabel(nodes, "B1");
    const b3Id = findNodeIdByLabel(nodes, "B3");

    // 2. Simulate drag: drop B3 onto B1's slot (moves B3 above B1).
    const rewire = rewireBlockDropInScope({
      nodes,
      edges,
      scope: TOP_LEVEL_SCOPE,
      activeId: b3Id,
      overId: b1Id,
    });
    expect(rewire).not.toBeNull();

    // 3. Save: feed the rewired edges back through getWorkflowBlocks.
    const savedAfterDrop = getWorkflowBlocks(nodes, rewire!.edges);
    expect(savedAfterDrop.map((b) => b.label)).toEqual([
      "B3",
      "B1",
      "B2",
      "B4",
      "B5",
    ]);

    // 4. Chain order is the invariant we ship — the backend walks
    //    next_block_label to execute blocks, so this is ground truth.
    expect(chainFromSavedBlocks(savedAfterDrop)).toEqual([
      "B3",
      "B1",
      "B2",
      "B4",
      "B5",
    ]);

    // 5. Backend `_build_loop_graph` invariants hold on the saved YAML.
    expect(() =>
      assertBackendBuildLoopGraphAccepts(savedAfterDrop),
    ).not.toThrow();

    // 6. Reload: feed the saved YAML back through getElements, save again,
    //    and assert the chain is stable. Catches edge-reconstruction bugs
    //    where the reloaded graph drifts from its persisted form.
    const { nodes: reloadedNodes, edges: reloadedEdges } =
      reloadFromSavedYaml(savedAfterDrop);
    const savedAfterReload = getWorkflowBlocks(reloadedNodes, reloadedEdges);
    expect(chainFromSavedBlocks(savedAfterReload)).toEqual([
      "B3",
      "B1",
      "B2",
      "B4",
      "B5",
    ]);
    expect(() =>
      assertBackendBuildLoopGraphAccepts(savedAfterReload),
    ).not.toThrow();
  });

  test("after drag + reload the workflow start edge targets the chain root, not blocks[0]", () => {
    // Repro for the screenshot in D0AJR0MCVJ9 thread 1778566586.727119:
    // drag B3 above B1, save, reload. The previous bug emitted blocks[] in
    // node-array order ([B1,B2,B3,B4,B5]) so getElements connected the
    // workflow start to blocks[0] = B1 while B3 (the real chain root) hung
    // as a floating block with a single B3 -> B1 edge.
    const initialBlocks = buildFiveBlockFixture();
    const { nodes, edges } = getElements(initialBlocks, DEFAULT_SETTINGS, true);

    const b1Id = findNodeIdByLabel(nodes, "B1");
    const b3Id = findNodeIdByLabel(nodes, "B3");

    const rewire = rewireBlockDropInScope({
      nodes,
      edges,
      scope: TOP_LEVEL_SCOPE,
      activeId: b3Id,
      overId: b1Id,
    });
    expect(rewire).not.toBeNull();

    const savedAfterDrop = getWorkflowBlocks(nodes, rewire!.edges);
    // Lock array order: the chain root must be first so the loader's
    // blocks[0] read at workflowEditorUtils.ts:1863 picks it up.
    expect(savedAfterDrop[0]!.label).toBe("B3");

    const { nodes: reloadedNodes, edges: reloadedEdges } =
      reloadFromSavedYaml(savedAfterDrop);
    const startNode = reloadedNodes.find(
      (n) => n.type === "start" && !n.parentId,
    );
    expect(startNode).toBeDefined();

    const edgesFromStart = reloadedEdges.filter(
      (e) => e.source === startNode!.id,
    );
    expect(edgesFromStart).toHaveLength(1);

    const reloadedB3 = findNodeIdByLabel(reloadedNodes, "B3");
    expect(edgesFromStart[0]!.target).toBe(reloadedB3);

    // B3 must have exactly one inbound chain edge - if blocks[0] were stale
    // and the loader emitted both start -> B1 and B3 -> B1, B3 itself would
    // have zero inbound edges (the floating orphan case).
    const inboundToB3 = reloadedEdges.filter((e) => e.target === reloadedB3);
    expect(inboundToB3).toHaveLength(1);
    expect(inboundToB3[0]!.source).toBe(startNode!.id);
  });

  test("save is a no-op fixed point for an already-saved workflow", () => {
    // Sanity: without a drop, the round-trip shouldn't mutate the chain.
    // If this ever fails, either getElements or getWorkflowBlocks has
    // drifted — no drop should be required to surface that.
    const initialBlocks = buildFiveBlockFixture();
    const { nodes, edges } = getElements(initialBlocks, DEFAULT_SETTINGS, true);

    const savedOnce = getWorkflowBlocks(nodes, edges);
    const { nodes: reloadedNodes, edges: reloadedEdges } =
      reloadFromSavedYaml(savedOnce);
    const savedTwice = getWorkflowBlocks(reloadedNodes, reloadedEdges);

    expect(chainFromSavedBlocks(savedTwice)).toEqual(
      chainFromSavedBlocks(savedOnce),
    );
  });

  test("load is order-invariant: stale array still resolves the chain root via adjacency", () => {
    // Simulates an upstream writer (paste, AI import, server-side sort) that
    // produced a valid (B, E) chain B3 -> B1 -> B2 -> B4 -> B5 but persisted
    // blocks[] in unsorted order [B1,B2,B3,B4,B5]. Approach B's loader must
    // still pick B3 as the root.
    const blocks: Array<WorkflowBlock> = [
      makeCodeBlock("B1", "B2"),
      makeCodeBlock("B2", "B4"),
      makeCodeBlock("B3", "B1"),
      makeCodeBlock("B4", "B5"),
      makeCodeBlock("B5", null),
    ];

    const { nodes, edges } = getElements(blocks, DEFAULT_SETTINGS, true);

    const startNode = nodes.find((n) => n.type === "start" && !n.parentId);
    expect(startNode).toBeDefined();

    const edgesFromStart = edges.filter((e) => e.source === startNode!.id);
    expect(edgesFromStart).toHaveLength(1);

    const b3Id = findNodeIdByLabel(nodes, "B3");
    expect(edgesFromStart[0]!.target).toBe(b3Id);

    const inboundToB3 = edges.filter((e) => e.target === b3Id);
    expect(inboundToB3).toHaveLength(1);
    expect(inboundToB3[0]!.source).toBe(startNode!.id);
  });

  test("loop loader chains by next_block_label, not loop_blocks[] index", () => {
    // Loop with chain L3 -> L1 -> L2 but persisted array [L1, L2, L3].
    // Approach C's loader must walk the chain.
    const l1 = makeCodeBlock("L1", "L2");
    const l2 = makeCodeBlock("L2", null);
    const l3 = makeCodeBlock("L3", "L1");
    const loop: WorkflowBlock = {
      label: "FOR1",
      block_type: "for_loop",
      continue_on_failure: false,
      model: null,
      next_block_label: null,
      output_parameter: makeOutputParameter("FOR1"),
      loop_over: { key: "items" } as never,
      loop_blocks: [l1, l2, l3],
      loop_variable_reference: null,
      complete_if_empty: false,
      data_schema: null,
    } as never;

    const { nodes, edges } = getElements([loop], DEFAULT_SETTINGS, true);

    const loopId = findNodeIdByLabel(nodes, "FOR1");
    const loopStart = nodes.find(
      (n) => n.type === "start" && n.parentId === loopId,
    );
    expect(loopStart).toBeDefined();

    const fromLoopStart = edges.filter((e) => e.source === loopStart!.id);
    expect(fromLoopStart).toHaveLength(1);

    const l3Id = findNodeIdByLabel(nodes, "L3");
    expect(fromLoopStart[0]!.target).toBe(l3Id);

    const l1Id = findNodeIdByLabel(nodes, "L1");
    const l2Id = findNodeIdByLabel(nodes, "L2");
    expect(edges.find((e) => e.source === l3Id)?.target).toBe(l1Id);
    expect(edges.find((e) => e.source === l1Id)?.target).toBe(l2Id);
    const loopAdder = nodes.find(
      (n) => n.type === "nodeAdder" && n.parentId === loopId,
    );
    expect(loopAdder).toBeDefined();
    expect(edges.find((e) => e.source === l2Id)?.target).toBe(loopAdder!.id);
  });

  test("strict load (editable=true) surfaces WorkflowValidationError as validationError on malformed input", () => {
    const malformed: Array<WorkflowBlock> = [
      makeCodeBlock("B1", "DOES_NOT_EXIST"),
    ];
    const { validationError } = getElements(malformed, DEFAULT_SETTINGS, true);
    expect(validationError).not.toBeNull();
    expect(validationError!.message).toMatch(
      /references unknown next_block_label/,
    );
  });

  test("permissive load (editable=false) returns null validationError on malformed input", () => {
    const malformed: Array<WorkflowBlock> = [
      makeCodeBlock("B1", "DOES_NOT_EXIST"),
    ];
    const { validationError } = getElements(malformed, DEFAULT_SETTINGS, false);
    expect(validationError).toBeNull();
  });

  // error_code_mapping is not editable in YAML, but it must still ride on the
  // start node so it's preserved (not cleared) across a load -> save round-trip.
  test("workflow-level error_code_mapping rides on the start node so it survives a save", () => {
    const settings: WorkflowSettings = {
      ...DEFAULT_SETTINGS,
      errorCodeMapping: { OUT_OF_STOCK: "item unavailable" },
    };
    const { nodes } = getElements(buildFiveBlockFixture(), settings, true);
    const startNode = nodes.find((node) => node.type === "start");
    expect(
      (startNode?.data as { errorCodeMapping?: unknown } | undefined)
        ?.errorCodeMapping,
    ).toEqual({ OUT_OF_STOCK: "item unavailable" });
  });

  // The full recovery leg the save path relies on: workflow-level settings ride
  // load -> start node -> getWorkflowSettings with zero field loss, so a YAML
  // commit (which reattaches settings from this readback) cannot drop them.
  test("workflow-level settings survive the getElements -> getWorkflowSettings round-trip", () => {
    const settings: WorkflowSettings = {
      ...DEFAULT_SETTINGS,
      errorCodeMapping: { OUT_OF_STOCK: "item unavailable" },
      finallyBlockLabel: "B5",
      workflowSystemPrompt: "always double-check totals",
    };
    const { nodes } = getElements(buildFiveBlockFixture(), settings, true);
    const recovered = getWorkflowSettings(nodes);
    expect(recovered.errorCodeMapping).toEqual({
      OUT_OF_STOCK: "item unavailable",
    });
    expect(recovered.finallyBlockLabel).toBe("B5");
    expect(recovered.workflowSystemPrompt).toBe("always double-check totals");
  });
});
