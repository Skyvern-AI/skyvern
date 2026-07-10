import { useEdges, useNodes, useNodesData } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";

import { helpTooltips } from "../../helpContent";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { type AppNode } from "..";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { useUpdate } from "../../useUpdate";
import { type SplitPdfNode, type SplitPdfNodeData } from "./types";

function SplitPdfEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<SplitPdfNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "splitPdf") {
    return null;
  }
  return <SplitPdfEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function SplitPdfEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: SplitPdfNodeData;
}) {
  const { editable } = data;
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const update = useUpdate<SplitPdfNodeData>({ id: blockId, editable });

  return (
    <div data-testid="split-pdf-block-form" className="space-y-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">File URL</Label>
          <HelpTooltip content={helpTooltips["split_pdf"]["fileUrl"]} />
        </div>
        <WorkflowBlockInput
          nodeId={blockId}
          value={data.fileUrl}
          onChange={(next) => update({ fileUrl: next })}
          className="nopan text-xs"
        />
      </div>

      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Prompt</Label>
          <HelpTooltip content={helpTooltips["split_pdf"]["prompt"]} />
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          value={data.prompt}
          onChange={(next) => update({ prompt: next })}
          className="nopan text-xs"
          placeholder="Split the PDF into one file per document."
        />
      </div>

      <ModelSelector
        className="nopan w-52 text-xs"
        value={data.model}
        onChange={(value) => update({ model: value })}
      />

      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <div className="space-y-4">
              <ParametersMultiSelect
                availableOutputParameters={availableOutputParameterKeys}
                parameters={data.parameterKeys}
                onParametersChange={(parameterKeys) => {
                  update({ parameterKeys });
                }}
              />
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
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">LLM Key</Label>
                  <HelpTooltip content={helpTooltips["split_pdf"]["llmKey"]} />
                </div>
                <WorkflowBlockInput
                  nodeId={blockId}
                  value={data.llmKey}
                  onChange={(next) => update({ llmKey: next })}
                  className="nopan text-xs"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { SplitPdfEditor };
