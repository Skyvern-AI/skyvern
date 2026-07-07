import { ExclamationTriangleIcon, PlusIcon } from "@radix-ui/react-icons";
import { useEdges, useNodes, useNodesData } from "@xyflow/react";
import { useCallback } from "react";

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
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

import { helpTooltips } from "../../helpContent";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { type AppNode } from "..";
import { JsonValidator } from "../HttpRequestNode/HttpUtils";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { WorkflowBlockParameterSelect } from "../WorkflowBlockParameterSelect";
import { useUpdate } from "../../useUpdate";
import { type PdfFillNode, type PdfFillNodeData } from "./types";

function PdfFillEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<PdfFillNode>(blockId);
  if (!nodeSlice || nodeSlice.type !== "pdfFill") {
    return null;
  }
  return <PdfFillEditorBody blockId={blockId} data={nodeSlice.data} />;
}

function PdfFillEditorBody({
  blockId,
  data,
}: {
  blockId: string;
  data: PdfFillNodeData;
}) {
  const { editable } = data;
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const availableOutputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    blockId,
  );
  const update = useUpdate<PdfFillNodeData>({ id: blockId, editable });

  const handleAddParameterToPayload = useCallback(
    (parameterKey: string) => {
      const parameterSyntax = `{{ ${parameterKey} | json }}`;
      const currentPayload = data.payload || "{}";
      try {
        const parsed = JSON.parse(currentPayload);
        const existingKeys = Object.keys(parsed);
        let keyIndex = existingKeys.length + 1;
        let newKey = `param_${keyIndex}`;
        while (existingKeys.includes(newKey)) {
          keyIndex++;
          newKey = `param_${keyIndex}`;
        }
        parsed[newKey] = parameterSyntax;
        update({ payload: JSON.stringify(parsed, null, 2) });
      } catch {
        update({
          payload: JSON.stringify({ param_1: parameterSyntax }, null, 2),
        });
      }
    },
    [data.payload, update],
  );

  return (
    <div data-testid="pdf-fill-block-form" className="space-y-4">
      <p className="flex items-start gap-1.5 text-xs text-amber-400">
        <ExclamationTriangleIcon className="mt-0.5 size-3 shrink-0" />
        <span>
          Works best on fillable PDFs with form fields. Unstructured PDFs
          (scanned or flat documents) are filled best-effort and may be less
          reliable.
        </span>
      </p>
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">File URL</Label>
          <HelpTooltip content={helpTooltips["pdf_fill"]["fileUrl"]} />
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
          <HelpTooltip content={helpTooltips["pdf_fill"]["prompt"]} />
        </div>
        <WorkflowBlockInputTextarea
          nodeId={blockId}
          value={data.prompt}
          onChange={(next) => update({ prompt: next })}
          className="nopan text-xs"
          placeholder="Fill the form using the payload."
        />
      </div>

      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">Payload</Label>
            <HelpTooltip content={helpTooltips["pdf_fill"]["payload"]} />
          </div>
          <Popover>
            <PopoverTrigger asChild>
              <Button
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                disabled={!editable}
              >
                <PlusIcon className="mr-1 h-3 w-3" />
                Add Input
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-[22rem]">
              <WorkflowBlockParameterSelect
                nodeId={blockId}
                onAdd={handleAddParameterToPayload}
              />
            </PopoverContent>
          </Popover>
        </div>
        <CodeEditor
          className="w-full"
          language="json"
          value={data.payload}
          onChange={(next) => update({ payload: next || "{}" })}
          readOnly={!editable}
          minHeight="120px"
          maxHeight="220px"
        />
        <JsonValidator value={data.payload} />
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
                  <HelpTooltip content={helpTooltips["pdf_fill"]["llmKey"]} />
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

export { PdfFillEditor };
