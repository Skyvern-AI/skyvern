import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { helpTooltipContent, type ValidationNode } from "./types";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { useState } from "react";
import { CheckCircledIcon } from "@radix-ui/react-icons";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { Label } from "@/components/ui/label";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Checkbox } from "@/components/ui/checkbox";
import { errorMappingExampleValue } from "../types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { Switch } from "@/components/ui/switch";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Separator } from "@/components/ui/separator";

function ValidationNode({ id, data }: NodeProps<ValidationNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    completeCriterion: data.completeCriterion,
    terminateCriterion: data.terminateCriterion,
    errorCodeMapping: data.errorCodeMapping,
  });
  const deleteNodeCallback = useDeleteNodeCallback();

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
        <header className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <CheckCircledIcon className="size-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Validation Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </header>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Complete if...</Label>
          <AutoResizingTextarea
            onChange={(event) => {
              if (!editable) {
                return;
              }
              handleChange("completeCriterion", event.target.value);
            }}
            value={inputs.completeCriterion}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Terminate if...</Label>
          <AutoResizingTextarea
            onChange={(event) => {
              if (!editable) {
                return;
              }
              handleChange("terminateCriterion", event.target.value);
            }}
            value={inputs.terminateCriterion}
            className="nopan text-xs"
          />
        </div>
        <Separator />
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent>
              <div className="ml-6 mt-4 space-y-4">
                <div className="space-y-2">
                  <div className="flex gap-4">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Error Messages
                      </Label>
                      <HelpTooltip
                        content={helpTooltipContent["errorCodeMapping"]}
                      />
                    </div>
                    <Checkbox
                      checked={inputs.errorCodeMapping !== "null"}
                      disabled={!editable}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        handleChange(
                          "errorCodeMapping",
                          checked
                            ? JSON.stringify(errorMappingExampleValue, null, 2)
                            : "null",
                        );
                      }}
                    />
                  </div>
                  {inputs.errorCodeMapping !== "null" && (
                    <div>
                      <CodeEditor
                        language="json"
                        value={inputs.errorCodeMapping}
                        onChange={(value) => {
                          if (!editable) {
                            return;
                          }
                          handleChange("errorCodeMapping", value);
                        }}
                        className="nowheel nopan"
                        fontSize={8}
                      />
                    </div>
                  )}
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Continue on Failure
                    </Label>
                    <HelpTooltip
                      content={helpTooltipContent["continueOnFailure"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={data.continueOnFailure}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        updateNodeData(id, { continueOnFailure: checked });
                      }}
                    />
                  </div>
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>
      </div>
    </div>
  );
}

export { ValidationNode };
