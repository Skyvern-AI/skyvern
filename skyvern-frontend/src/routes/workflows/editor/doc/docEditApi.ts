import type { Edge } from "@xyflow/react";
import { createBranchCondition } from "../nodes/ConditionalNode/types";
import { nanoid } from "nanoid";
import { toast } from "@/components/ui/use-toast";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useCopilotActionStore } from "@/store/useCopilotActionStore";
import { useLocateBlockStore } from "../runValidation/useLocateBlockStore";
import { stripJinjaReferences } from "./docTextUtils";
import { useNodeCollapseStore } from "../collapse/useNodeCollapseStore";
import {
  isWorkflowBlockNode,
  type AppNode,
  type WorkflowBlockNode,
} from "../nodes";
import { nodeLibraryTitle } from "./docStepChoices";
import type { AddNodeProps } from "../Workspace";
import { demonstrationInsertionFromNodes } from "../workspaceAuthoringActions";
import type { WorkflowCopilotDemonstrationInsertion } from "../../copilot/workflowCopilotTypes";
import {
  getUpdatedParametersAfterLabelUpdateForSourceParameterKey,
  inferBranchCriteriaTypeFromExpression,
} from "../workflowEditorUtils";
import type { DocItem } from "./buildDocTree";
import {
  docMainIsSetting,
  docStepIsNoteOnly,
  getDocProseFields,
} from "./docStepRegistry";
import { withLineSetting } from "./docLineSettings";
import { useDocUnfinishedStore } from "./docUnfinished";
import {
  addBranch,
  checkLabel,
  clearLongDefaultText,
  insertionPoint,
  moveBlock,
  moveBranch,
  newStepLabel,
  labelStem,
  branchMembers,
  contextInputsFedBy,
  findOutputUsers,
  hasSettingsBeyondText,
  isBlankStep,
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
  const insertStep = (
    place: DocPlace,
    nodeType: NodeType,
    replacing?: string,
  ): string | null => {
    // A replaced step leaves the graph in the same pass that adds its successor: one layout, one undo step.
    const base = replacing ? removeBlock(graph, replacing, finallyId) : null;
    const point = insertionPoint(
      base ?? graph,
      place.scope,
      place.index,
      finallyId,
    );
    if (!point) return null;
    const added = edits.addStep({
      nodeType,
      ...point,
      replacing: base ? replacing : undefined,
      label: newStepLabel(
        nodes,
        nodeLibraryTitle(nodeType),
        useWorkflowParametersStore
          .getState()
          .parameters.map((parameter) => parameter.key),
      ),
      select: false,
    });
    if (added) {
      useDocUnfinishedStore.getState().add(added);
      edits.update((current) => clearLongDefaultText(current, added));
    }
    return added;
  };

  // A recording starts only in the top-level sequence; inside a loop or branch there is no insertion.
  const demonstrationInsertion = (
    place: DocPlace,
    replacing?: string,
  ): WorkflowCopilotDemonstrationInsertion | null => {
    if (place.scope.parentId || place.scope.conditionalBranchId) return null;
    // The empty step being replaced is not a neighbour: the recording goes where it stood.
    const without =
      replacing && canReplaceBlankStep(replacing)
        ? removeBlock(graph, replacing, finallyId)
        : null;
    const base = without ?? graph;
    const point = insertionPoint(base, place.scope, place.index, finallyId);
    return point ? demonstrationInsertionFromNodes(base.nodes, point) : null;
  };

  // Drops an empty step so a demonstration can take its place; false when it holds anything.
  const canReplaceBlankStep = (id: string): boolean => {
    const node = nodes.find((n) => n.id === id);
    if (!node || !isBlankStep(node)) return false;
    const { parameters } = useWorkflowParametersStore.getState();
    // Removing the step would strip the {{ step_output }} references other steps hold.
    return (
      !contextInputsFedBy(nodes, parameters, id).length &&
      !findOutputUsers(nodes, id).length
    );
  };

  const removeBlankStep = (id: string): boolean => {
    if (!canReplaceBlankStep(id)) return false;
    const next = removeBlock(graph, id, finallyId);
    if (!next) return false;
    edits.apply(next);
    return true;
  };

  return {
    demonstrationInsertion,
    removeBlankStep,
    canReplaceBlankStep,
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
    // A loop or branch with no step yet has nothing to hold a note: the answer is its scope, for the caller to open the
    // "/" step menu there. No step type is picked for the person.
    nestNote: (
      note: DocNoteRef,
      outdent: boolean,
    ): DocNoteRef | { pickIn: DocScopeRef } | null => {
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
      if (!last) return { pickIn: scope };
      return moveNoteText(
        note,
        { ownerNodeId: last, placement: "trailing" },
        false,
      );
    },
    // The picked type is the person's choice; the note's text becomes the new step's main text.
    adoptNote: (
      note: DocNoteRef,
      scope: DocScopeRef,
      nodeType: NodeType,
    ): string | null => {
      const text = noteText(note.ownerNodeId, note.placement).trim();
      // No immediate capture: the new step's layout passes land after it, and only the history's debounce
      // folds them, the step and the note's move into one undo entry.
      const stepId = insertStep({ scope, index: 0 }, nodeType);
      // A loop or If takes its list or condition from its own fields, and a step with no instruction field (Wait,
      // Print Page, ...) keeps its description as a note the agent never reads. The note stays where it is for both,
      // so the workflow's runtime context does not change.
      if (stepId && text)
        edits.update((current) => {
          const added = current.find((n) => n.id === stepId);
          if (
            !added ||
            docStepIsNoteOnly(added) ||
            added.type === "loop" ||
            added.type === "conditional"
          )
            return current;
          const key =
            (!docMainIsSetting(added) && getDocProseFields(added)[0]?.key) ||
            "description";
          return setStepField(
            setStepField(
              current,
              note.ownerNodeId,
              noteKey(note.placement),
              "",
            ),
            stepId,
            key,
            text,
          ).map((n) => (n.id === stepId ? withLineSetting(n, key, "") : n));
        });
      return stepId;
    },
    // Enter in a step: a step of the same type goes below it, and the text after the caret moves into it.
    splitStep: (
      place: DocPlace,
      id: string,
      split: { key: string; before: string; after: string } | null,
    ): string | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node || !isWorkflowBlockNode(node)) return null;
      // A URL or file is not split between two steps: Enter just adds the step below.
      if (docMainIsSetting(node)) split = null;
      // Enter with the caret at the very start of a step's text adds an empty step above and leaves the text, with
      // the step's settings, where it is. The caret stays in the original step.
      if (split && !split.before && split.after) {
        const above = insertStep(
          { scope: place.scope, index: place.index },
          node.type,
        );
        return above ? id : null;
      }
      const added = insertStep(
        { scope: place.scope, index: place.index + 1 },
        node.type,
      );
      // The original keeps only what is before the caret, even when a selection to the end leaves nothing after it.
      if (added && split)
        edits.update((current) => {
          // A new step can use another field for its text than the one split (engine defaults differ).
          const created = current.find((n) => n.id === added);
          const addedKey =
            (created && getDocProseFields(created)[0]?.key) || split.key;
          const previous = String(
            (node.data as Record<string, unknown>)[split.key] ?? "",
          );
          return setStepField(
            setStepField(current, id, split.key, split.before),
            added,
            addedKey,
            split.after,
          ).map((n) =>
            n.id === id
              ? withLineSetting(n, split.key, previous)
              : n.id === added
                ? withLineSetting(n, addedKey, "")
                : n,
          );
        });
      return added;
    },
    insertStep,
    // The empty step `id` becomes a step of `nodeType` in the same place.
    replaceStep: (id: string, nodeType: NodeType): string | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node || id === finallyId) return null;
      const scope = scopeOfNode(node);
      const index = readChain(graph, scope, finallyId)?.ids.indexOf(id) ?? -1;
      return index < 0 ? null : insertStep({ scope, index }, nodeType, id);
    },
    duplicate: (id: string) => edits.duplicate(id),
    // The step that keyboard focus moves to once `id` is deleted: the one above, else below, else its loop or If.
    neighbourOf: (id: string): string | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node) return null;
      // The finally step is left out of a chain that pins it, so its own chain is read without the pin.
      const chain =
        readChain(graph, scopeOfNode(node), id === finallyId ? null : finallyId)
          ?.ids ?? [];
      const at = chain.indexOf(id);
      if (at < 0) return node.parentId ?? null;
      const pinned = !node.parentId && id !== finallyId ? finallyId : null;
      return chain[at - 1] ?? chain[at + 1] ?? node.parentId ?? pinned ?? null;
    },
    // False when the step stays.
    remove: (id: string): boolean => {
      if (refuseIfFeedsInputs([id])) return false;
      const removed = nodes.find((n) => n.id === id);
      const next = removeBlock(graph, id, finallyId);
      if (!next) {
        refuse("This step can't be deleted here. Open Visual to edit it.");
        return false;
      }
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
      return true;
    },
    // A step still named for its old type, with nothing using its output, is renamed for the new type.
    turnInto: (id: string, nodeType: NodeType) => {
      const next = turnInto(graph, id, nodeType);
      if (!next) return;
      const node = nodes.find((n) => n.id === id);
      const titleOf = nodeLibraryTitle;
      const old = node && isWorkflowBlockNode(node) ? node : null;
      const autoNamed =
        old &&
        new RegExp(
          `^${labelStem(titleOf(old.type)).replace(/[^a-z0-9_]/g, "")}_\\d+$`,
        ).test(old.data.label);
      if (
        !old ||
        !autoNamed ||
        id === finallyId ||
        findOutputUsers(nodes, id).length
      )
        return edits.apply(next);
      const parameterKeys = useWorkflowParametersStore
        .getState()
        .parameters.map((parameter) => parameter.key);
      const label = newStepLabel(
        next.nodes.filter((n) => n.id !== id),
        titleOf(nodeType),
        parameterKeys,
      );
      if (checkLabel(next.nodes, id, label, parameterKeys))
        return edits.apply(next);
      const parameters = useWorkflowParametersStore.getState();
      parameters.setParameters(
        getUpdatedParametersAfterLabelUpdateForSourceParameterKey(
          id,
          label,
          next.nodes,
          parameters.parameters,
        ),
      );
      useNodeCollapseStore
        .getState()
        .renameBlock(workflowId, old.data.label, label);
      edits.apply({
        nodes: renameStep(next.nodes, id, label).nodes,
        edges: next.edges,
      });
    },
    move,
    // Tab nests a step in the loop or branch just above it; Shift+Tab puts it after the step that holds it.
    // Says what it moved through, or null when there is nowhere to go, so the key keeps its usual meaning.
    nest: (id: string, outdent: boolean): DocNestKind | null => {
      const node = nodes.find((n) => n.id === id);
      if (!node || id === finallyId) return null;
      if (outdent) {
        const parent = nodes.find((n) => n.id === node.parentId);
        const kind = nestKindOf(parent);
        if (!parent || !kind) return null;
        const scope = scopeOfNode(parent);
        const at = readChain(graph, scope, finallyId)?.ids.indexOf(parent.id);
        if (at === undefined || at < 0) return null;
        return move(id, { scope, index: at + 1 }) ? kind : null;
      }
      const chain = readChain(graph, scopeOfNode(node), finallyId);
      const at = chain?.ids.indexOf(id) ?? -1;
      const above =
        at > 0 ? nodes.find((n) => n.id === chain!.ids[at - 1]) : null;
      if (above?.type === "loop") {
        return move(id, {
          scope: { parentId: above.id, conditionalBranchId: null },
          index: Number.MAX_SAFE_INTEGER,
        })
          ? "loop"
          : null;
      }
      if (above?.type === "conditional") {
        const branchId =
          selectedBranch(above.id) ?? above.data.branches[0]?.id ?? null;
        if (!branchId) return null;
        return move(id, {
          scope: { parentId: above.id, conditionalBranchId: branchId },
          index: Number.MAX_SAFE_INTEGER,
        })
          ? "branch"
          : null;
      }
      return null;
    },
    // What a line moving in or out of `id` is moving through; with `holder`, of the loop or If that holds `id`.
    nestKind: (id: string | null, holder = false): DocNestKind | null => {
      const node = nodes.find((n) => n.id === id);
      return nestKindOf(
        holder ? nodes.find((n) => n.id === node?.parentId) : node,
      );
    },
    // A step with no text, no note and no setting: "/" can replace it outright.
    isEmptyStep: (id: string) => {
      const node = nodes.find((n) => n.id === id);
      if (
        !node ||
        !isWorkflowBlockNode(node) ||
        hasSettingsBeyondText(node) ||
        findOutputUsers(nodes, id).length ||
        contextInputsFedBy(
          nodes,
          useWorkflowParametersStore.getState().parameters,
          id,
        ).length
      )
        return false;
      const data = node.data as Record<string, unknown>;
      return [
        ...getDocProseFields(node).map((f) => f.key),
        "description",
        "notes",
        "trailingNotes",
      ].every((key) => {
        const value = data[key];
        return typeof value !== "string" || !value.trim();
      });
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
      if (end.kind === "forward-reference") {
        const users = Array.from(new Set(end.referrerLabels));
        return refuse(
          `${end.movedLabel} can't run at the end: ${users.join(", ")} ${users.length === 1 ? "uses" : "use"} its output.`,
        );
      }
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

export type DocNestKind = "loop" | "branch";

function nestKindOf(node: AppNode | undefined): DocNestKind | null {
  if (node?.type === "loop") return "loop";
  if (node?.type === "conditional") return "branch";
  return null;
}
