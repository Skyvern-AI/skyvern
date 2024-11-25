import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { HelpTooltip } from "@/components/HelpTooltip";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import { dataSchemaExampleValue } from "../types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { Switch } from "@/components/ui/switch";
import type { ExtractionNode } from "./types";
import {
  commonFieldPlaceholders,
  commonHelpTooltipContent,
} from "../../constants";
import { ExtractIcon } from "@/components/icons/ExtractIcon";

const dataExtractionGoalTooltip =
  "Tell Skyvern what data you would like to scrape.";
const dataSchemaTooltip = "Specify a format for extracted data in JSON.";
const dataExtractionGoalPlaceholder = "What data do you need to extract?";

function ExtractionNode({ id, data }: NodeProps<ExtractionNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    url: data.url,
    dataExtractionGoal: data.dataExtractionGoal,
    dataSchema: data.dataSchema,
    maxRetries: data.maxRetries,
    maxStepsOverride: data.maxStepsOverride,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
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
              <ExtractIcon className="size-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Extraction Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </header>
        <div className="space-y-2">
          <div className="flex gap-2">
            <Label className="text-xs text-slate-300">
              Data Extraction Goal
            </Label>
            <HelpTooltip content={dataExtractionGoalTooltip} />
          </div>
          <AutoResizingTextarea
            onChange={(event) => {
              if (!editable) {
                return;
              }
              handleChange("dataExtractionGoal", event.target.value);
            }}
            value={inputs.dataExtractionGoal}
            placeholder={dataExtractionGoalPlaceholder}
            className="nopan text-xs"
          />
        </div>
        <div className="space-y-2">
          <div className="flex gap-4">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Data Schema</Label>
              <HelpTooltip content={dataSchemaTooltip} />
            </div>
            <Checkbox
              checked={inputs.dataSchema !== "null"}
              onCheckedChange={(checked) => {
                if (!editable) {
                  return;
                }
                handleChange(
                  "dataSchema",
                  checked
                    ? JSON.stringify(dataSchemaExampleValue, null, 2)
                    : "null",
                );
              }}
            />
          </div>
          {inputs.dataSchema !== "null" && (
            <div>
              <CodeEditor
                language="json"
                value={inputs.dataSchema}
                onChange={(value) => {
                  if (!editable) {
                    return;
                  }
                  handleChange("dataSchema", value);
                }}
                className="nowheel nopan"
                fontSize={8}
              />
            </div>
          )}
        </div>
        <Separator />
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger className="py-0">
              Advanced Settings
            </AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Max Retries
                    </Label>
                    <HelpTooltip
                      content={commonHelpTooltipContent["maxRetries"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={commonFieldPlaceholders["maxRetries"]}
                    className="nopan w-52 text-xs"
                    min="0"
                    value={inputs.maxRetries ?? ""}
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      const value =
                        event.target.value === ""
                          ? null
                          : Number(event.target.value);
                      handleChange("maxRetries", value);
                    }}
                  />
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Max Steps Override
                    </Label>
                    <HelpTooltip
                      content={commonHelpTooltipContent["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={commonFieldPlaceholders["maxStepsOverride"]}
                    className="nopan w-52 text-xs"
                    min="0"
                    value={inputs.maxStepsOverride ?? ""}
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      const value =
                        event.target.value === ""
                          ? null
                          : Number(event.target.value);
                      handleChange("maxStepsOverride", value);
                    }}
                  />
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Continue on Failure
                    </Label>
                    <HelpTooltip
                      content={commonHelpTooltipContent["continueOnFailure"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.continueOnFailure}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        handleChange("continueOnFailure", checked);
                      }}
                    />
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Cache Actions
                    </Label>
                    <HelpTooltip
                      content={commonHelpTooltipContent["cacheActions"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.cacheActions}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
                        handleChange("cacheActions", checked);
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

export { ExtractionNode };
