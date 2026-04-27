import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Handle, NodeProps, Position } from "@xyflow/react";
import { helpTooltips } from "../../helpContent";
import { type FileParserNode, type FileParserFileType } from "./types";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { cn } from "@/util/utils";
import { NodeHeader } from "../components/NodeHeader";
import { useParams } from "react-router-dom";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";
import { dataSchemaExampleForFileExtraction } from "../types";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useUpdate } from "@/routes/workflows/editor/useUpdate";
import { ModelSelector } from "@/components/ModelSelector";
import { useRecordingStore } from "@/store/useRecordingStore";
import { Separator } from "@/components/ui/separator";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";

const FILE_TYPE_OPTIONS: Array<{ value: FileParserFileType; label: string }> = [
  { value: "auto_detect", label: "Auto detect" },
  { value: "csv", label: "CSV" },
  { value: "excel", label: "Excel" },
  { value: "pdf", label: "PDF" },
  { value: "image", label: "Image" },
  { value: "docx", label: "DOCX" },
];

const FILE_EXTENSION_TO_TYPE: Record<string, FileParserFileType> = {
  csv: "csv",
  xlsx: "excel",
  xls: "excel",
  pdf: "pdf",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  docx: "docx",
};

function detectFileTypeFromUrl(url: string): FileParserFileType | null {
  try {
    const urlObj = new URL(url);
    const pathname = urlObj.pathname;
    const ext = pathname.split(".").pop()?.toLowerCase();
    if (ext && ext in FILE_EXTENSION_TO_TYPE) {
      return FILE_EXTENSION_TO_TYPE[ext] ?? null;
    }
  } catch {
    // Not a valid URL; try plain extension match
    const ext = url.split(".").pop()?.toLowerCase().split("?")[0];
    if (ext && ext in FILE_EXTENSION_TO_TYPE) {
      return FILE_EXTENSION_TO_TYPE[ext] ?? null;
    }
  }
  return null;
}

function FileParserNode({ id, data }: NodeProps<FileParserNode>) {
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
  const update = useUpdate<FileParserNode["data"]>({ id, editable });
  const recordingStore = useRecordingStore();

  function handleFileUrlChange(value: string) {
    const detected = detectFileTypeFromUrl(value);
    const currentType = data.fileType;
    const canInfer = detected && (!currentType || currentType === detected);
    if (canInfer) {
      update({ fileUrl: value, fileType: detected });
    } else {
      update({ fileUrl: value });
    }
  }

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
              value={data.fileUrl}
              onChange={handleFileUrlChange}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">File Type</Label>
              <HelpTooltip content={helpTooltips["fileParser"]["fileType"]} />
            </div>
            <Select
              value={data.fileType}
              onValueChange={(value) => {
                update({ fileType: value as FileParserFileType });
              }}
              disabled={!editable}
            >
              <SelectTrigger className="nopan w-36 text-xs">
                <SelectValue placeholder="Select type" />
              </SelectTrigger>
              <SelectContent>
                {FILE_TYPE_OPTIONS.map((option) => (
                  <SelectItem
                    key={option.value}
                    value={option.value}
                    className="text-xs"
                  >
                    {option.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <WorkflowDataSchemaInputGroup
            exampleValue={dataSchemaExampleForFileExtraction}
            value={data.jsonSchema}
            onChange={(value) => {
              update({ jsonSchema: value });
            }}
            suggestionContext={{ current_schema: data.jsonSchema }}
          />
          <ModelSelector
            className="nopan w-52 text-xs"
            value={data.model}
            onChange={(value) => {
              update({ model: value });
            }}
          />
          <Separator />
          <IgnoreWorkflowSystemPrompt
            ignoreWorkflowSystemPrompt={
              data.ignoreWorkflowSystemPrompt ?? false
            }
            editable={editable}
            onIgnoreWorkflowSystemPromptChange={(
              ignoreWorkflowSystemPrompt,
            ) => {
              update({ ignoreWorkflowSystemPrompt });
            }}
          />
        </div>
      </div>
    </div>
  );
}

export { FileParserNode };
