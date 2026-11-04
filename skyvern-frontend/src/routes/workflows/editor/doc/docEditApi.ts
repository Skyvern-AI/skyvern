import type { Edge } from "@xyflow/react";
import { createBranchCondition } from "../nodes/ConditionalNode/types";
import { nanoid } from "nanoid";
import { toast } from "@/components/ui/use-toast";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
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
import {
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
  inferBranchCriteriaTypeFromExpression,
} from "../workflowEditorUtils";
import type { DocItem } from "./buildDocTree";
import { withLineSetting } from "./docLineSettings";
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
  const noteKey = (placement: DocNoteRef["placement"]) =>
    placement === "above" ? "notes" : "trailingNotes";
  const noteText = (id: string, placement: DocNoteRef["placement"]) => {
    const value = (
      nodes.find((n) => n.id === id)?.data as Record<string, unknown>
    )?.[noteKey(placement)];
    return typeof value === "string" ? value : "";
  };
  // The loop, or the shown branch of the If, a line can move into.
  const innerScope = (container: AppNode | undefined): DocScopeRef | null => {
    if (container?.type === "loop")
      return { parentId: container.id, conditionalBranchId: null };
    if (container?.type === "conditional") {
      const branchId =
        selectedBranch(container.id) ?? container.data.branches[0]?.id;
      return branchId
        ? { parentId: container.id, conditionalBranchId: branchId }
        : null;
    }
    return null;
  };
  const moveNoteText = (
    from: DocNoteRef,
    to: DocNoteRef,
    toFront: boolean,
  ): DocNoteRef | null => {
    const moving = noteText(from.ownerNodeId, from.placement).trim();
    if (moving) {
      const existing = noteText(to.ownerNodeId, to.placement).trim();
      const joined = [toFront ? moving : existing, toFront ? existing : moving]
        .filter(Boolean)
        .join("\n");
      if (joined.length > NOTE_MAX_LENGTH) {
        refuse(
          `The notes together would be over ${NOTE_MAX_LENGTH.toLocaleString("en-US")} characters. Shorten one first.`,
        );
        return null;
      }
      let next = setStepField(
        nodes,
        from.ownerNodeId,
        noteKey(from.placement),
        "",
      );
      next = setStepField(next, to.ownerNodeId, noteKey(to.placement), joined);
      edits.apply({ nodes: next, edges });
    }
    return to;
  };
  const insertStep = (place: DocPlace, nodeType: NodeType): string | null => {
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
    if (added) edits.update((current) => clearLongDefaultText(current, added));
    return added;
  };

  return {
    setText: (id: string, key: string, value: string) =>
      edits.update((current) => {
        const before = current.find((n) => n.id === id)?.data as
          | Record<string, unknown>
          | undefined;
        // A build in flight carries the Goal it started with, so a later edit would be lost to it (the Visual editor disables the field).
        if (key === "prompt" && typeof before?.label === "string") {
          const { generatingBlockLabel, queuedBuilds } =
            useCopilotActionStore.getState();
          if (
            current.find((n) => n.id === id)?.type === "codeBlock" &&
            (generatingBlockLabel === before.label ||
              queuedBuilds.some((queued) => queued.blockLabel === before.label))
          )
            return current;
        }
        const previous =
          typeof before?.[key] === "string" ? (before[key] as string) : "";
        return setStepField(current, id, key, value).map((n) =>
          n.id === id ? withLineSetting(n, key, previous) : n,
        );
      }),
    // The If line is the branch's condition. Its check follows what was typed, as the save reads it.
    setCondition: (conditionalId: string, branchId: string, value: string) =>
      edits.update((current) =>
        current.map((n) =>
          n.id === conditionalId && n.type === "conditional"
            ? ({
                ...n,
                data: {
                  ...n.data,
                  branches: n.data.branches.map((branch) =>
                    branch.id === branchId && branch.criteria
                      ? {
                          ...branch,
                          criteria: {
                            ...branch.criteria,
                            expression: value,
                            criteria_type:
                              inferBranchCriteriaTypeFromExpression(value),
                          },
                        }
                      : branch,
                  ),
                },
              } as AppNode)
            : n,
        ),
      ),
    stepBefore: (id: string): string | null => {
      const node = nodes.find((n) => n.id === id);
      const chain = node
        ? (readChain(graph, scopeOfNode(node), finallyId)?.ids ?? [])
        : [];
      return chain[chain.indexOf(id) - 1] ?? null;
    },
    // Tab moves a note into the loop or If above it, after its last step; Shift+Tab moves the note after
    // the last step of a loop or branch out, under the loop or If. Null when there is nowhere to go.
    // A loop or branch with no step yet has nothing to hold a note, so the note becomes its first step.
    nestNote: (
      note: DocNoteRef,
      outdent: boolean,
    ): DocNoteRef | { stepId: string } | null => {
      const owner = nodes.find((n) => n.id === note.ownerNodeId);
      if (!owner) return null;
      const ownerScope = scopeOfNode(owner);
      const chain = readChain(graph, ownerScope, finallyId)?.ids ?? [];
      if (outdent) {
        const parent = nodes.find((n) => n.id === owner.parentId);
        if (
          !parent ||
          note.placement !== "trailing" ||
          chain[chain.length - 1] !== owner.id
        )
          return null;
        return moveNoteText(
          note,
          { ownerNodeId: parent.id, placement: "trailing" },
          true,
        );
      }
      const container =
        note.placement === "trailing"
          ? owner
          : nodes.find((n) => n.id === chain[chain.indexOf(owner.id) - 1]);
      const scope = innerScope(container);
      if (!scope) return null;
      const inside = readChain(graph, scope, finallyId)?.ids ?? [];
      const last = inside[inside.length - 1];
      if (!last) {
        const text = noteText(note.ownerNodeId, note.placement).trim();
        // A Browser Task is the step whose main text is an instruction, as the Doc's "/" menu offers it.
        // No immediate capture here: the new step's layout passes land after it, and only the history's debounce
        // folds them, the step and the note's move into one undo entry.
        const stepId = insertStep({ scope, index: 0 }, "navigation");
        if (!stepId) return null;
        if (text)
          edits.update((current) =>
            setStepField(
              setStepField(
                current,
                note.ownerNodeId,
                noteKey(note.placement),
                "",
              ),
              stepId,
              "navigationGoal",
              text,
            ),
          );
        return { stepId };
      }
      return moveNoteText(
        note,
        { ownerNodeId: last, placement: "trailing" },
        false,
      );
    },
    insertStep,
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
