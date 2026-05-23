import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import { helpTooltips } from "../../helpContent";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { type AppNode, isWorkflowBlockNode } from "..";
import {
  isFileParserNode,
  type FileParserFileType,
  type FileParserNode,
} from "./types";
import { dataSchemaExampleForFileExtraction } from "../types";
import { useUpdate } from "../../useUpdate";

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
    const ext = url.split(".").pop()?.toLowerCase().split("?")[0];
    if (ext && ext in FILE_EXTENSION_TO_TYPE) {
      return FILE_EXTENSION_TO_TYPE[ext] ?? null;
    }
  }
  return null;
}

function FileParserEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isFileParserNode(node)) {
    return null;
  }
  return (
    <FileParserEditorBody blockId={blockId} node={node as FileParserNode} />
  );
}

function FileParserEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: FileParserNode;
}) {
  const data = node.data;
  const editable = data.editable;
  const update = useUpdate<FileParserNode["data"]>({ id: blockId, editable });

  const handleFileUrlChange = (value: string) => {
    const detected = detectFileTypeFromUrl(value);
    const currentType = data.fileType;
    const canInfer =
      detected &&
      (!currentType ||
        currentType === "auto_detect" ||
        currentType === detected);
    if (canInfer) {
      update({ fileUrl: value, fileType: detected });
    } else {
      update({ fileUrl: value });
    }
  };

  return (
    <div data-testid="file-parser-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">File URL</Label>
          <HelpTooltip content={helpTooltips["fileParser"]["fileUrl"]} />
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
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
        onChange={(value) => update({ jsonSchema: value })}
        suggestionContext={{ current_schema: data.jsonSchema }}
      />
      <ModelSelector
        className="nopan w-52 text-xs"
        value={data.model}
        onChange={(value) => update({ model: value })}
      />
      <IgnoreWorkflowSystemPrompt
        ignoreWorkflowSystemPrompt={data.ignoreWorkflowSystemPrompt ?? false}
        editable={editable}
        onIgnoreWorkflowSystemPromptChange={(ignoreWorkflowSystemPrompt) => {
          update({ ignoreWorkflowSystemPrompt });
        }}
      />
    </div>
  );
}

export { FileParserEditor };
