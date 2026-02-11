import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { dataSchemaExampleForFileExtraction } from "../types";
import { type PDFParserNode } from "./types";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { ModelSelector } from "@/components/ModelSelector";
import { useRecordingStore } from "@/store/useRecordingStore";

function PDFParserNode({ id, data }: NodeProps<PDFParserNode>) {
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
  const update = useUpdate<PDFParserNode["data"]>({ id, editable });
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
          type="pdf_parser" // sic: the naming is not consistent
        />
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">File URL</Label>
                <HelpTooltip content={helpTooltips["pdfParser"]["fileUrl"]} />
              </div>
              {isFirstWorkflowBlock ? (
                <div className="flex justify-end text-xs text-slate-400">
                  Tip: Use the {"+"} button to add parameters!
                </div>
              ) : null}
            </div>
            <WorkflowBlockInput
              nodeId={id}
              value={data.fileUrl}
              onChange={(value) => {
                update({ fileUrl: value });
              }}
              className="nopan text-xs"
            />
          </div>
          <WorkflowDataSchemaInputGroup
            exampleValue={dataSchemaExampleForFileExtraction}
            value={data.jsonSchema}
            onChange={(value) => {
              update({ jsonSchema: value });
            }}
            suggestionContext={{}}
          />
          <ModelSelector
            className="nopan w-52 text-xs"
            value={data.model}
            onChange={(value) => {
              update({ model: value });
            }}
          />
        </div>
      </div>
    </div>
  );
}

export { PDFParserNode };
