import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { type TextPromptNode } from "./types";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { dataSchemaExampleValue } from "../types";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { ModelSelector } from "@/components/ModelSelector";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { AI_IMPROVE_CONFIGS } from "../../constants";
import { useRecordingStore } from "@/store/useRecordingStore";

function TextPromptNode({ id, data }: NodeProps<TextPromptNode>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRunIsRunningOrQueued && thisBlockIsTargetted;
  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });
  const update = useUpdate<TextPromptNode["data"]>({ id, editable });
  const recordingStore = useRecordingStore();

  return (
    <div
      className={cn({
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
    >
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
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="text_prompt" // sic: the naming is not consistent
        />
        <div className="space-y-2">
          <div className="flex justify-between">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Prompt</Label>
              <HelpTooltip content={helpTooltips["textPrompt"]["prompt"]} />
            </div>
            {isFirstWorkflowBlock ? (
              <div className="flex justify-end text-xs text-slate-400">
                Tip: Use the {"+"} button to add parameters!
              </div>
            ) : null}
          </div>

          <WorkflowBlockInputTextarea
            aiImprove={AI_IMPROVE_CONFIGS.textPrompt.prompt}
            nodeId={id}
            onChange={(value) => {
              update({ prompt: value });
            }}
            value={data.prompt}
            placeholder="What do you want to generate?"
            className="nopan text-xs"
          />
        </div>
        <Separator />
        <ModelSelector
          className="nopan w-52 text-xs"
          value={data.model}
          onChange={(value) => {
            update({ model: value });
          }}
        />
        <WorkflowDataSchemaInputGroup
          exampleValue={dataSchemaExampleValue}
          value={data.jsonSchema}
          onChange={(value) => {
            update({ jsonSchema: value });
          }}
          suggestionContext={{}}
        />
      </div>
    </div>
  );
}

export { TextPromptNode };
