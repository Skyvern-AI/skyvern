import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips } from "../../helpContent";
import { type FileParserNode } from "./types";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { useDebugStore } from "@/store/useDebugStore";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { dataSchemaExampleForFileExtraction } from "../types";

function FileParserNode({ id, data }: NodeProps<FileParserNode>) {
  const { updateNodeData } = useReactFlow();
  const { debuggable, editable, label } = data;
  const debugStore = useDebugStore();
  const elideFromDebugging = debugStore.isDebugMode && !debuggable;
  const { blockLabel: urlBlockLabel } = useParams();
  const thisBlockIsPlaying =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const [inputs, setInputs] = useState({
    fileUrl: data.fileUrl,
    jsonSchema: data.jsonSchema,
  });

  function handleChange(key: string, value: unknown) {
    if (!data.editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

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
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-all",
          {
            "pointer-events-none bg-slate-950 outline outline-2 outline-slate-300":
              thisBlockIsPlaying,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          disabled={elideFromDebugging}
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="file_url_parser" // sic: the naming is not consistent
        />
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex justify-between">
              <div className="flex gap-2">
                <Label className="text-xs text-slate-300">File URL</Label>
                <HelpTooltip content={helpTooltips["fileParser"]["fileUrl"]} />
              </div>
              {isFirstWorkflowBlock ? (
                <div className="flex justify-end text-xs text-slate-400">
                  Tip: Use the {"+"} button to add parameters!
                </div>
              ) : null}
            </div>

            <WorkflowBlockInput
              nodeId={id}
              value={inputs.fileUrl}
              onChange={(value) => {
                handleChange("fileUrl", value);
              }}
              className="nopan text-xs"
            />
          </div>
          <WorkflowDataSchemaInputGroup
            exampleValue={dataSchemaExampleForFileExtraction}
            value={inputs.jsonSchema}
            onChange={(value) => {
              handleChange("jsonSchema", value);
            }}
            suggestionContext={{}}
          />
        </div>
      </div>
    </div>
  );
}

export { FileParserNode };
