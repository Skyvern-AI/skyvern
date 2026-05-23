import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import { helpTooltips } from "../../helpContent";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { type AppNode, isWorkflowBlockNode } from "..";
import type { PDFParserNode, PDFParserNodeData } from "./types";
import { dataSchemaExampleForFileExtraction } from "../types";
import { useUpdate } from "../../useUpdate";

function PDFParserEditor({ blockId }: { blockId: string }) {
  const rf = useReactFlow<AppNode>();
  const node = rf.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || node.type !== "pdfParser") {
    return null;
  }
  return <PDFParserEditorBody blockId={blockId} node={node as PDFParserNode} />;
}

function PDFParserEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: PDFParserNode;
}) {
  const { editable, fileUrl, jsonSchema, model } = node.data;
  const update = useUpdate<PDFParserNodeData>({ id: blockId, editable });

  return (
    <div data-testid="pdf-parser-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">File URL</Label>
          <HelpTooltip content={helpTooltips["pdfParser"]["fileUrl"]} />
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
          value={fileUrl}
          onChange={(v) => update({ fileUrl: v })}
          className="nopan text-xs"
        />
      </div>
      <WorkflowDataSchemaInputGroup
        exampleValue={dataSchemaExampleForFileExtraction}
        value={jsonSchema}
        onChange={(v) => update({ jsonSchema: v })}
        suggestionContext={{ current_schema: jsonSchema }}
      />
      <ModelSelector
        className="nopan w-52 text-xs"
        value={model}
        onChange={(v) => update({ model: v })}
      />
      <IgnoreWorkflowSystemPrompt
        ignoreWorkflowSystemPrompt={
          node.data.ignoreWorkflowSystemPrompt ?? false
        }
        editable={editable}
        onIgnoreWorkflowSystemPromptChange={(ignoreWorkflowSystemPrompt) => {
          update({ ignoreWorkflowSystemPrompt });
        }}
      />
    </div>
  );
}

export { PDFParserEditor };
