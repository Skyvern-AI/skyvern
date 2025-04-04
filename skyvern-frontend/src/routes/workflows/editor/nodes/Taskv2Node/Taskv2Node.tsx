import { HelpTooltip } from "@/components/HelpTooltip";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { WorkflowBlockTypes } from "@/routes/workflows/types/workflowTypes";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { helpTooltips, placeholders } from "../../helpContent";
import { useIsFirstBlockInWorkflow } from "../../hooks/useIsFirstNodeInWorkflow";
import { NodeActionMenu } from "../NodeActionMenu";
import { WorkflowBlockIcon } from "../WorkflowBlockIcon";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { MAX_STEPS_DEFAULT, type Taskv2Node } from "./types";

function Taskv2Node({ id, data, type }: NodeProps<Taskv2Node>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  const isFirstWorkflowBlock = useIsFirstBlockInWorkflow({ id });

  const [inputs, setInputs] = useState({
    prompt: data.prompt,
    url: data.url,
    totpVerificationUrl: data.totpVerificationUrl,
    totpIdentifier: data.totpIdentifier,
    maxSteps: data.maxSteps,
  });

  function handleChange(key: string, value: unknown) {
    if (!editable) {
      return;
    }
    setInputs({ ...inputs, [key]: value });
    updateNodeData(id, { [key]: value });
  }

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
      <div className="w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={WorkflowBlockTypes.Taskv2}
                className="size-6"
              />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">
                Navigation v2 Block
              </span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex justify-between">
              <Label className="text-xs text-slate-300">Prompt</Label>
              {isFirstWorkflowBlock ? (
                <div className="flex justify-end text-xs text-slate-400">
                  Tip: Use the {"+"} button to add parameters!
                </div>
              ) : null}
            </div>
            <WorkflowBlockInputTextarea
              nodeId={id}
              onChange={(value) => {
                handleChange("prompt", value);
              }}
              value={inputs.prompt}
              placeholder={placeholders[type]["prompt"]}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <Label className="text-xs text-slate-300">URL</Label>
            <WorkflowBlockInputTextarea
              nodeId={id}
              onChange={(value) => {
                handleChange("url", value);
              }}
              value={inputs.url}
              placeholder={placeholders[type]["url"]}
              className="nopan text-xs"
            />
          </div>
        </div>
        <Separator />
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-4">
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">Max Steps</Label>
                    <HelpTooltip content={helpTooltips[type]["maxSteps"]} />
                  </div>
                  <Input
                    type="number"
                    placeholder="10"
                    className="nopan text-xs"
                    value={data.maxSteps ?? MAX_STEPS_DEFAULT}
                    onChange={(event) => {
                      handleChange("maxSteps", Number(event.target.value));
                    }}
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Identifier
                    </Label>
                    <HelpTooltip
                      content={helpTooltips[type]["totpIdentifier"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    nodeId={id}
                    onChange={(value) => {
                      handleChange("totpIdentifier", value);
                    }}
                    value={inputs.totpIdentifier ?? ""}
                    placeholder={placeholders["navigation"]["totpIdentifier"]}
                    className="nopan text-xs"
                  />
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}

export { Taskv2Node };
