import { PlusIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useEdges } from "@xyflow/react";

import { useProcessRecordingMutation } from "@/routes/browserSessions/hooks/useProcessRecordingMutation";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordedBlocksStore } from "@/store/RecordedBlocksStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useSettingsStore } from "@/store/SettingsStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { cn } from "@/util/utils";

import type { NodeAdderNode } from "./types";
import { WorkflowAddMenu } from "../../WorkflowAddMenu";
import { WorkflowAdderBusy } from "../../WorkflowAdderBusy";

function NodeAdderNode({ id, parentId }: NodeProps<NodeAdderNode>) {
  const edges = useEdges();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const settingsStore = useSettingsStore();
  const setWorkflowPanelState = useWorkflowPanelStore(
    (state) => state.setWorkflowPanelState,
  );
  const workflowStatePanel = useWorkflowPanelStore();
  const setRecordedBlocks = useRecordedBlocksStore(
    (state) => state.setRecordedBlocks,
  );

  const previous = edges.find((edge) => edge.target === id)?.source ?? null;

  const processRecordingMutation = useProcessRecordingMutation({
    browserSessionId: settingsStore.browserSessionId,
    onSuccess: (result) => {
      setRecordedBlocks(result, {
        previous,
        next: id,
        parent: parentId,
        connectingEdgeType: "default",
      });
    },
  });

  const isProcessing = processRecordingMutation.isPending;

  const isBusy =
    (isProcessing || recordingStore.isRecording) &&
    debugStore.isDebugMode &&
    settingsStore.isUsingABrowser &&
    workflowStatePanel.workflowPanelState.data?.previous === previous &&
    workflowStatePanel.workflowPanelState.data?.next === id &&
    workflowStatePanel.workflowPanelState.data?.parent ===
      (parentId || undefined);

  const isDisabled = !isBusy && recordingStore.isRecording;

  const updateWorkflowPanelState = (active: boolean) => {
    const previous = edges.find((edge) => edge.target === id)?.source;

    setWorkflowPanelState({
      active,
      content: "nodeLibrary",
      data: {
        previous: previous ?? null,
        next: id,
        parent: parentId,
        connectingEdgeType: "default",
      },
    });
  };

  const onAdd = () => {
    if (isDisabled) {
      return;
    }

    updateWorkflowPanelState(true);
  };

  const onRecord = () => {
    if (recordingStore.isRecording) {
      recordingStore.setIsRecording(false);
    } else {
      recordingStore.setIsRecording(true);
      updateWorkflowPanelState(false);
    }
  };

  const onEndRecord = () => {
    if (recordingStore.isRecording) {
      recordingStore.setIsRecording(false);
    }

    processRecordingMutation.mutate();
  };

  const adder = (
    <div
      className={cn("rounded-full bg-slate-50 p-2", {
        "cursor-not-allowed bg-[grey]": isDisabled,
      })}
      onClick={() => {
        onAdd();
      }}
    >
      <PlusIcon className="h-12 w-12 text-slate-950" />
    </div>
  );

  const busy = (
    <WorkflowAdderBusy
      color={isProcessing ? "gray" : "red"}
      operation={isProcessing ? "processing" : "recording"}
      onComplete={() => {
        onEndRecord();
      }}
    >
      {adder}
    </WorkflowAdderBusy>
  );

  const menu = (
    <WorkflowAddMenu onAdd={onAdd} onRecord={onRecord}>
      {adder}
    </WorkflowAddMenu>
  );

  return (
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      {isBusy ? busy : isDisabled ? adder : menu}
    </div>
  );
}

export { NodeAdderNode };
