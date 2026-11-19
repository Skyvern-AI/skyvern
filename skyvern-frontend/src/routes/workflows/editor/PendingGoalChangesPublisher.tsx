import { useNodes, useReactFlow } from "@xyflow/react";
import { useCallback, useEffect } from "react";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  goalActionIsLocked,
  useEmberActionStore,
} from "@/store/useEmberActionStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { refuseMutationDuringYamlCommit } from "@/store/WorkflowYamlEditorStore";

import { isWorkflowBlockNode, type AppNode } from "./nodes";
import type { CodeBlockNode } from "./nodes/CodeBlockNode/types";
import { useWorkflowScopeReadOnly } from "./WorkflowScopeContext";
import {
  acceptGoalSuggestionPatch,
  codeEditedBlocksOf,
  codeEditedNoticeIsShown,
  goalChangeIsPending,
  goalChangeUndoPatch,
  keepCodeWithGoalPatch,
  pendingGoalChangesOf,
} from "./workflowEditorUtils";

type GoalSuggestionResponse = { goal: string | null };

export function PendingGoalChangesPublisher() {
  const nodes = useNodes<AppNode>();
  const { getNodes, updateNodeData } = useReactFlow<AppNode>();
  const credentialGetter = useCredentialGetter();
  const scopeReadOnly = useWorkflowScopeReadOnly();
  const setPendingGoalChanges = useEmberActionStore(
    (state) => state.setPendingGoalChanges,
  );
  const setUndoGoalChange = useEmberActionStore(
    (state) => state.setUndoGoalChange,
  );
  const goalSuggestions = useEmberActionStore((state) => state.goalSuggestions);
  const setCodeEditedBlocks = useEmberActionStore(
    (state) => state.setCodeEditedBlocks,
  );
  const setReadOnlyGoalLabels = useEmberActionStore(
    (state) => state.setReadOnlyGoalLabels,
  );
  const setCodeEditedGoalActions = useEmberActionStore(
    (state) => state.setCodeEditedGoalActions,
  );

  const codeBlockLabelled = useCallback(
    (label: string): CodeBlockNode | null => {
      const node = getNodes().find(
        (candidate) =>
          isWorkflowBlockNode(candidate) &&
          candidate.type === "codeBlock" &&
          candidate.data.label === label,
      );
      return node && node.type === "codeBlock" ? node : null;
    },
    [getNodes],
  );

  // The same rules as a field edit (useUpdate) plus the Goal field's lock while its block builds; a
  // Goal change the person made outranks a suggestion written before it.
  const refuseGoalAction = useCallback(
    (node: CodeBlockNode, { offersGoal }: { offersGoal: boolean }) => {
      if (
        goalActionIsLocked(useEmberActionStore.getState(), node.data.label, {
          readOnly: !node.data.editable || scopeReadOnly,
          mutationLocked: false,
        })
      ) {
        return true;
      }
      if (offersGoal && goalChangeIsPending(node.data)) {
        return true;
      }
      return refuseMutationDuringYamlCommit();
    },
    [scopeReadOnly],
  );

  const applyPatch = useCallback(
    (node: CodeBlockNode, patch: Partial<CodeBlockNode["data"]>) => {
      useWorkflowTitleStore.getState().recordEmberGraphEdit();
      updateNodeData(node.id, patch);
    },
    [updateNodeData],
  );

  const undoGoalChange = useCallback(
    (label: string) => {
      const node = codeBlockLabelled(label);
      if (!node || refuseGoalAction(node, { offersGoal: false })) {
        return;
      }
      const patch = goalChangeUndoPatch(node.data);
      if (patch) {
        applyPatch(node, patch);
      }
    },
    [codeBlockLabelled, refuseGoalAction, applyPatch],
  );

  const updateGoal = useCallback(
    async (label: string) => {
      const node = codeBlockLabelled(label);
      const store = useEmberActionStore.getState();
      if (
        !node ||
        !codeEditedNoticeIsShown(node.data) ||
        store.suggestingGoalLabels.includes(label) ||
        refuseGoalAction(node, { offersGoal: true })
      ) {
        return;
      }
      const forCode = node.data.code;
      const forGoal = node.data.prompt ?? "";
      store.setSuggestingGoal(label, true);
      try {
        const client = await getClient(credentialGetter, "sans-api-v1");
        const response = await client.post<GoalSuggestionResponse>(
          "/workflow/suggest-goal",
          {
            label,
            code: forCode,
            current_goal: forGoal,
            parameter_keys: node.data.parameterKeys ?? [],
          },
        );
        const goal = response.data.goal;
        const live = codeBlockLabelled(label);
        if (!live || !codeEditedNoticeIsShown(live.data)) {
          return;
        }
        if (goal) {
          store.setGoalSuggestion(label, { forCode, forGoal, goal });
        } else {
          toast({ title: "No Goal could be written from this code" });
        }
      } catch {
        toast({ title: "Couldn't suggest a Goal", variant: "destructive" });
      } finally {
        useEmberActionStore.getState().setSuggestingGoal(label, false);
      }
    },
    [codeBlockLabelled, refuseGoalAction, credentialGetter],
  );

  const keepGoal = useCallback(
    (label: string) => {
      const node = codeBlockLabelled(label);
      if (!node || refuseGoalAction(node, { offersGoal: false })) {
        return;
      }
      useEmberActionStore.getState().setGoalSuggestion(label, null);
      applyPatch(node, { codeEditedByHand: false });
    },
    [codeBlockLabelled, refuseGoalAction, applyPatch],
  );

  const acceptGoal = useCallback(
    (label: string) => {
      const node = codeBlockLabelled(label);
      if (!node || refuseGoalAction(node, { offersGoal: true })) {
        return;
      }
      const store = useEmberActionStore.getState();
      const patch = acceptGoalSuggestionPatch(
        node.data,
        store.goalSuggestions[label],
      );
      store.setGoalSuggestion(label, null);
      if (patch) {
        applyPatch(node, patch);
      }
    },
    [codeBlockLabelled, refuseGoalAction, applyPatch],
  );

  const keepCode = useCallback(
    (label: string) => {
      const node = codeBlockLabelled(label);
      if (!node || refuseGoalAction(node, { offersGoal: false })) {
        return;
      }
      const patch = keepCodeWithGoalPatch(node.data);
      if (patch) {
        applyPatch(node, patch);
      }
    },
    [codeBlockLabelled, refuseGoalAction, applyPatch],
  );

  useEffect(() => {
    setPendingGoalChanges(pendingGoalChangesOf(nodes));
    setCodeEditedBlocks(codeEditedBlocksOf(nodes, goalSuggestions));
    setReadOnlyGoalLabels(
      nodes.flatMap((node) =>
        isWorkflowBlockNode(node) &&
        node.type === "codeBlock" &&
        (scopeReadOnly || !node.data.editable)
          ? [node.data.label]
          : [],
      ),
    );
  }, [
    nodes,
    goalSuggestions,
    scopeReadOnly,
    setPendingGoalChanges,
    setCodeEditedBlocks,
    setReadOnlyGoalLabels,
  ]);
  useEffect(() => {
    setUndoGoalChange(undoGoalChange);
  }, [undoGoalChange, setUndoGoalChange]);
  useEffect(() => {
    setCodeEditedGoalActions({
      updateGoal: (label) => void updateGoal(label),
      keepGoal,
      acceptGoal,
      keepCode,
    });
  }, [updateGoal, keepGoal, acceptGoal, keepCode, setCodeEditedGoalActions]);
  useEffect(
    () => () => {
      setPendingGoalChanges([]);
      setUndoGoalChange(() => {});
      setCodeEditedBlocks([]);
      setReadOnlyGoalLabels([]);
      useEmberActionStore.setState({
        goalSuggestions: {},
        suggestingGoalLabels: [],
      });
      setCodeEditedGoalActions({
        updateGoal: () => {},
        keepGoal: () => {},
        acceptGoal: () => {},
        keepCode: () => {},
      });
    },
    [
      setPendingGoalChanges,
      setUndoGoalChange,
      setCodeEditedBlocks,
      setReadOnlyGoalLabels,
      setCodeEditedGoalActions,
    ],
  );
  return null;
}
