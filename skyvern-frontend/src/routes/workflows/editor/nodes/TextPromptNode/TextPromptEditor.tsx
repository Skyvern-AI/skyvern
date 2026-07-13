import { useReactFlow } from "@xyflow/react";

import { HelpTooltip } from "@/components/HelpTooltip";
import { ModelSelector } from "@/components/ModelSelector";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowDataSchemaInputGroup } from "@/components/DataSchemaInputGroup/WorkflowDataSchemaInputGroup";

import { AI_IMPROVE_CONFIGS } from "../../constants";
import { IgnoreWorkflowSystemPrompt } from "../IgnoreWorkflowSystemPrompt";
import { helpTooltips } from "../../helpContent";
import { type AppNode, isWorkflowBlockNode } from "..";
import {
  isTextPromptNode,
  type TextPromptNode,
  type TextPromptNodeData,
} from "./types";
import { dataSchemaExampleValue } from "../types";
import { useUpdate } from "../../useUpdate";

function TextPromptEditor({ blockId }: { blockId: string }) {
  const reactFlow = useReactFlow<AppNode>();
  const node = reactFlow.getNode(blockId);
  if (!node || !isWorkflowBlockNode(node) || !isTextPromptNode(node)) {
    return null;
  }
  return <TextPromptEditorBody blockId={blockId} node={node} />;
}

function TextPromptEditorBody({
  blockId,
  node,
}: {
  blockId: string;
  node: TextPromptNode;
}) {
  const { editable, prompt, jsonSchema, model } = node.data;
  const update = useUpdate<TextPromptNodeData>({ id: blockId, editable });

  return (
    <div data-testid="text-prompt-block-form" className="space-y-4 p-4">
      <div className="space-y-2">
        <div className="flex gap-2">
          <Label className="text-xs text-slate-300">Prompt</Label>
          <HelpTooltip content={helpTooltips["textPrompt"]["prompt"]} />
        </div>
        <WorkflowBlockInputTextarea
          aiImprove={AI_IMPROVE_CONFIGS.textPrompt.prompt}
          nodeId={blockId}
          value={prompt}
          onChange={(next) => update({ prompt: next })}
          placeholder="What do you want to generate?"
          className="nopan text-xs"
        />
      </div>
      <Separator />
      <ModelSelector
        className="nopan w-52 text-xs"
        value={model}
        onChange={(next) => update({ model: next })}
      />
      <WorkflowDataSchemaInputGroup
        exampleValue={dataSchemaExampleValue}
        value={jsonSchema}
        onChange={(next) => update({ jsonSchema: next })}
        suggestionContext={{ current_schema: jsonSchema }}
      />
      <Accordion type="single" collapsible>
        <AccordionItem value="advanced" className="border-b-0">
          <AccordionTrigger className="py-0">
            Advanced Settings
          </AccordionTrigger>
          <AccordionContent className="pl-6 pr-1 pt-1">
            <IgnoreWorkflowSystemPrompt
              ignoreWorkflowSystemPrompt={
                node.data.ignoreWorkflowSystemPrompt ?? false
              }
              editable={editable}
              onIgnoreWorkflowSystemPromptChange={(
                ignoreWorkflowSystemPrompt,
              ) => {
                update({ ignoreWorkflowSystemPrompt });
              }}
            />
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </div>
  );
}

export { TextPromptEditor };
