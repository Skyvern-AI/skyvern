import type { Edge } from "@xyflow/react";
import { createBranchCondition } from "../nodes/ConditionalNode/types";
import { nanoid } from "nanoid";
import { toast } from "@/components/ui/use-toast";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { stripJinjaReferences } from "./docTextUtils";
import { showDropBlockedToast } from "../sortable/dropBlockedToast";
import { useNodeCollapseStore } from "../collapse/useNodeCollapseStore";
import {
  isWorkflowBlockNode,
  type AppNode,
  type WorkflowBlockNode,
} from "../nodes";
import { availableNodeLibraryItems } from "../panels/nodeLibraryItems";
import type { AddNodeProps } from "../Workspace";
import { getUpdatedParametersAfterLabelUpdateForSourceParameterKey } from "../workflowEditorUtils";
import type { DocItem } from "./buildDocTree";
import {
  addBranch,
  checkLabel,
  clearLongDefaultText,
  insertionPoint,
  moveBlock,
  moveBranch,
  newStepLabel,
  branchMembers,
  contextInputsFedBy,
  findOutputUsers,
  hasSettingsBeyondText,
  NOTE_MAX_LENGTH,
  removeBlock,
  readChain,
  referencesAt,
  removeBranch,
  renameStep,
  scopeOfNode,
  toNote,
  setStepField,
  turnInto,
  type DocGraph,
  type DocNoteRef,
  type DocScopeRef,
} from "./docGraphEdits";

/** What FlowRenderer lets the Doc do to the shared graph. */
export type DocGraphEdits = {
  // A structural change: one undo step, re-laid out.
  apply: (graph: DocGraph) => void;
  // A text change, applied to the latest graph: grouped into undo steps by the history's pause debounce.
  update: (change: (nodes: AppNode[]) => AppNode[]) => void;
  // Returns the new step's id, or null when nothing was added.
  addStep: (props: AddNodeProps) => string | null;
  duplicate: (id: string) => void;
};

export type DocPlace = { scope: DocScopeRef; index: number };
export type DocDestination = DocPlace & {
  label: string;
  insideOf: string | null;
};
export type NodeType = NonNullable<WorkflowBlockNode["type"]>;

export const TOP_SCOPE: DocScopeRef = {
  parentId: null,
  conditionalBranchId: null,
};

function preview(text: string) {
  const plain = stripJinjaReferences(text).trim();
  return plain.length > 40 ? `${plain.slice(0, 39)}…` : plain;
}

/** Every place a step can move to, for "Move to…": the doc top and the start of each loop and branch. */
export function docDestinations(
  items: DocItem[],
  byId: Map<string, AppNode>,
  text: (node: AppNode) => string,
): DocDestination[] {
  const out: DocDestination[] = [
    { label: "Top of the doc", scope: TOP_SCOPE, index: 0, insideOf: null },
  ];
  const visit = (list: DocItem[]) => {
    for (const item of list) {
      if (item.kind === "note" || item.kind === "unplaced") continue;
      if (item.readOnlyReason) continue;
      const node = byId.get(item.nodeId);
      if (!node) continue;
      if (item.kind === "loop") {
        out.push({
          label: `Loop: ${preview(text(node)) || node.data.label}`,
          scope: { parentId: item.nodeId, conditionalBranchId: null },
          index: 0,
          insideOf: item.nodeId,
        });
        visit(item.children);
      }
      if (item.kind === "conditional") {
        const ifText =
          preview(item.branches[0]?.condition ?? "") || node.data.label;
        for (const branch of item.branches) {
          if (branch.synthetic || branch.jumpTo) continue;
          const name =
            branch.kind === "otherwise"
              ? "Otherwise"
              : `${branch.kind === "if" ? "If" : "Else if"} ${preview(branch.condition) || "no condition"}`;
          out.push({
            label: branch.kind === "if" ? name : `If ${ifText} › ${name}`,
            scope: {
              parentId: item.nodeId,
              conditionalBranchId: branch.branchId,
            },
            index: 0,
            insideOf: item.nodeId,
          });
          visit(branch.children);
        }
      }
    }
  };
  visit(items);
  return out;
}

function withFinallyLabel(nodes: AppNode[], label: string | null): AppNode[] {
  return nodes.map((n) =>
    n.type === "start" && !n.parentId && n.data.withWorkflowSettings
      ? ({ ...n, data: { ...n.data, finallyBlockLabel: label } } as AppNode)
      : n,
  );
}

export function createDocEditApi({
  nodes,
  edges,
  edits,
  finallyId,
  workflowId,
  selectBranch,
  selectedBranch,
  inputKeys,
  credentialKeys,
}: {
  nodes: AppNode[];
  edges: Edge[];
  edits: DocGraphEdits;
  finallyId: string | null;
  workflowId: string;
  selectBranch: (conditionalId: string, branchId: string) => void;
  selectedBranch: (conditionalId: string) => string | undefined;
  inputKeys: string[];
  credentialKeys: Set<string>;
}) {
  const graph: DocGraph = { nodes, edges };
  const refuse = (reason: string) => {
    if (reason) toast({ title: reason, variant: "destructive" });
  };
  // The save rejects a context input whose source step is gone, so deleting that step waits for the input.
  const refuseIfFeedsInputs = (ids: string[]): boolean => {
    const { parameters } = useWorkflowParametersStore.getState();
    const fed = Array.from(
      new Set(ids.flatMap((id) => contextInputsFedBy(nodes, parameters, id))),
    );
    if (!fed.length) return false;
    refuse(
      `The input ${fed.join(", ")} takes its value from this step's output. Change that input first.`,
    );
    return true;
  };
  const move = (id: string, place: DocPlace): boolean => {
    const result = moveBlock(graph, id, place.scope, place.index, finallyId);
    if (result.kind === "refused") {
      refuse(result.reason);
      return false;
    }
    if (result.kind === "forward-reference") {
      toast({
        title: `Can't move "${result.movedLabel}" here`,
        description: result.reason,
        variant: "destructive",
        type: "background",
      });
      return false;
    }
    edits.apply(result.graph);
    if (place.scope.conditionalBranchId && place.scope.parentId)
      selectBranch(place.scope.parentId, place.scope.conditionalBranchId);
    const parentId = place.scope.parentId;
    if (
      parentId &&
      nodes.some((node) => node.id === parentId && node.type === "loop")
    )
      useLocateBlockStore.getState().requestLocate(id, parentId);
    return true;
  };
  return {
    setText: (id: string, key: string, value: string) =>
      edits.update((current) => setStepField(current, id, key, value)),
    insertStep: (place: DocPlace, nodeType: NodeType): string | null => {
      const point = insertionPoint(graph, place.scope, place.index, finallyId);
      if (!point) return null;
      const added = edits.addStep({
        nodeType,
        ...point,
        label: newStepLabel(
          nodes,
          availableNodeLibraryItems().find((i) => i.nodeType === nodeType)
            ?.title ?? nodeType,
          useWorkflowParametersStore
            .getState()
            .parameters.map((parameter) => parameter.key),
        ),
        select: false,
      });
      if (added)
        edits.update((current) => clearLongDefaultText(current, added));
      return added;
    },
    duplicate: (id: string) => edits.duplicate(id),
    remove: (id: string) => {
      if (refuseIfFeedsInputs([id])) return;
      const removed = nodes.find((n) => n.id === id);
      const next = removeBlock(graph, id, finallyId);
      if (!next)
        return refuse(
          "This step can't be deleted here. Open Visual to edit it.",
        );
      // The save refuses a finally label that names no step.
      edits.apply(
        id === finallyId
          ? { ...next, nodes: withFinallyLabel(next.nodes, null) }
          : next,
      );
      toast({
        title:
          removed && isWorkflowBlockNode(removed)
            ? `Deleted ${removed.data.label}`
            : "Deleted",
      });
    },
    turnInto: (id: string, nodeType: NodeType) => {
      const next = turnInto(graph, id, nodeType);
      if (next) edits.apply(next);
    },
    move,
    // Tab nests a step in the loop or branch just above it; Shift+Tab puts it after the step that holds it.
    // False when there is nowhere to go, so the key keeps its usual meaning.
    nest: (id: string, outdent: boolean): boolean => {
      const node = nodes.find((n) => n.id === id);
      if (!node || id === finallyId) return false;
      if (outdent) {
        const parent = nodes.find((n) => n.id === node.parentId);
        if (!parent) return false;
        const scope = scopeOfNode(parent);
        const at = readChain(graph, scope, finallyId)?.ids.indexOf(parent.id);
        if (at === undefined || at < 0) return false;
        return move(id, { scope, index: at + 1 });
      }
      const chain = readChain(graph, scopeOfNode(node), finallyId);
      const at = chain?.ids.indexOf(id) ?? -1;
      const above =
        at > 0 ? nodes.find((n) => n.id === chain!.ids[at - 1]) : null;
      if (above?.type === "loop") {
        return move(id, {
          scope: { parentId: above.id, conditionalBranchId: null },
          index: Number.MAX_SAFE_INTEGER,
        });
      }
      if (above?.type === "conditional") {
        const branchId =
          selectedBranch(above.id) ?? above.data.branches[0]?.id ?? null;
        if (!branchId) return false;
        return move(id, {
          scope: { parentId: above.id, conditionalBranchId: branchId },
          index: Number.MAX_SAFE_INTEGER,
        });
      }
      return false;
    },
    // Only a step whose whole content is text can become text without losing anything.
    canBePlainText: (id: string) => {
      const node = nodes.find((n) => n.id === id);
      return Boolean(node) && !hasSettingsBeyondText(node!);
    },
    // Back to plain text; returns the note that now holds the text, for focus.
    toPlainText: (id: string): DocNoteRef | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node || hasSettingsBeyondText(node)) {
        refuse(
          "This step has settings plain text can't keep. Delete it from the step's menu instead.",
        );
        return null;
      }
      if (findOutputUsers(nodes, id).length) {
        refuse(
          "Other steps use this step's output. Delete it from the step's menu to see which.",
        );
        return null;
      }
      if (refuseIfFeedsInputs([id])) return null;
      const result = toNote(graph, id, finallyId);
      if (!result) {
        refuse(
          "Add another step next to this one first, so the text has a place to go.",
        );
        return null;
      }
      if (result.note) {
        const { ownerNodeId, placement } = result.note;
        const owner = result.graph.nodes.find((n) => n.id === ownerNodeId);
        const text = (owner?.data as Record<string, unknown> | undefined)?.[
          placement === "above" ? "notes" : "trailingNotes"
        ];
        if (typeof text === "string" && text.length > NOTE_MAX_LENGTH) {
          refuse(
            `The note would be over ${NOTE_MAX_LENGTH.toLocaleString("en-US")} characters. Shorten the step's text first.`,
          );
          return null;
        }
      }
      edits.apply(
        id === finallyId
          ? {
              ...result.graph,
              nodes: withFinallyLabel(result.graph.nodes, null),
            }
          : result.graph,
      );
      return result.note;
    },
    // Notes interpolate parameters, not block outputs, and reach the AI as plain text without credentials.
    references: (id: string, inNote: boolean) =>
      referencesAt(
        graph,
        id,
        inNote ? inputKeys.filter((k) => !credentialKeys.has(k)) : inputKeys,
        finallyId,
        !inNote,
      ),
    setFinally: (id: string, on: boolean) => {
      const label = nodes.find((n) => n.id === id)?.data.label;
      const next = withFinallyLabel(nodes, on ? (label ?? null) : null);
      if (!on) {
        // The display-only edge into the old finally step must become a real one, or the step saves unreachable.
        const real = edges.map((e) =>
          e.target === id &&
          (e.data as { synthetic?: boolean } | undefined)?.synthetic
            ? { ...e, data: { ...e.data, synthetic: false } }
            : e,
        );
        return edits.apply({ nodes: next, edges: real });
      }
      // The finally step runs last, so it moves to the end first.
      const end = moveBlock(
        { nodes: next, edges },
        id,
        TOP_SCOPE,
        Number.MAX_SAFE_INTEGER,
      );
      if (end.kind === "forward-reference")
        return showDropBlockedToast({
          kind: "forward-reference",
          movedBlockLabel: end.movedLabel,
          referrerLabels: end.referrerLabels,
        });
      edits.apply(end.kind === "ok" ? end.graph : { nodes: next, edges });
    },
    rename: (id: string, label: string): string | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node || !isWorkflowBlockNode(node) || node.data.label === label)
        return null;
      const parameterKeys = useWorkflowParametersStore
        .getState()
        .parameters.map((parameter) => parameter.key);
      const problem = checkLabel(nodes, id, label, parameterKeys);
      if (problem) return problem;
      const result = renameStep(nodes, id, label);
      const rewritten = result.rewritten;
      const renamed =
        id === finallyId ? withFinallyLabel(result.nodes, label) : result.nodes;
      const parameters = useWorkflowParametersStore.getState();
      parameters.setParameters(
        getUpdatedParametersAfterLabelUpdateForSourceParameterKey(
          id,
          label,
          nodes,
          parameters.parameters,
        ),
      );
      useNodeCollapseStore
        .getState()
        .renameBlock(workflowId, node.data.label, label);
      edits.apply({ nodes: renamed, edges });
      toast({
        title: `Renamed ${node.data.label} to ${label}`,
        ...(rewritten > 0 && {
          description: `${rewritten} reference${rewritten === 1 ? "" : "s"} updated`,
        }),
      });
      return null;
    },
    addBranch: (conditionalId: string) => {
      const id = nanoid();
      // Same shape as Visual's new branch: the save refuses a non-default branch with no criteria.
      const next = addBranch(
        graph,
        conditionalId,
        createBranchCondition({ id }),
      );
      if (!next) return;
      edits.apply(next);
      selectBranch(conditionalId, id);
    },
    moveBranch: (conditionalId: string, branchId: string, delta: -1 | 1) => {
      const next = moveBranch(graph, conditionalId, branchId, delta);
      if (next) edits.apply(next);
    },
    removeBranch: (conditionalId: string, branchId: string) => {
      const members = branchMembers(nodes, conditionalId, branchId);
      if (refuseIfFeedsInputs(members.map((member) => member.id))) return;
      const next = removeBranch(graph, conditionalId, branchId);
      if (next) {
        edits.apply(next);
        if (selectedBranch(conditionalId) === branchId) {
          const conditional = next.nodes.find(
            (node) => node.id === conditionalId,
          );
          if (conditional?.type === "conditional")
            selectBranch(
              conditionalId,
              conditional.data.activeBranchId ??
                conditional.data.branches[0]!.id,
            );
        }
      }
    },
  };
}

export type DocEditApi = ReturnType<typeof createDocEditApi>;
