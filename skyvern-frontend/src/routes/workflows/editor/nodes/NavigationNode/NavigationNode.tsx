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
import { errorMappingExampleValue } from "../types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { Switch } from "@/components/ui/switch";
import type { NavigationNode } from "./types";
import { RobotIcon } from "@/components/icons/RobotIcon";
import { helpTooltips, placeholders } from "../../helpContent";
import { WorkflowBlockParameterSelect } from "../WorkflowBlockParameterSelect";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowBlockInput } from "@/components/WorkflowBlockInput";

function NavigationNode({ id, data }: NodeProps<NavigationNode>) {
  const { updateNodeData } = useReactFlow();
  const [parametersPanelField, setParametersPanelField] = useState<
    string | null
  >(null);

  const { editable } = data;
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });
  const [inputs, setInputs] = useState({
    url: data.url,
    navigationGoal: data.navigationGoal,
    errorCodeMapping: data.errorCodeMapping,
    maxRetries: data.maxRetries,
    maxStepsOverride: data.maxStepsOverride,
    allowDownloads: data.allowDownloads,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
    downloadSuffix: data.downloadSuffix,
    totpVerificationUrl: data.totpVerificationUrl,
    totpIdentifier: data.totpIdentifier,
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
    <div className="relative">
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
              <RobotIcon className="size-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Navigation Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </header>
        <div className="space-y-4">
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">URL</Label>
              <HelpTooltip content={helpTooltips["navigation"]["url"]} />
            </div>
            <WorkflowBlockInputTextarea
              onIconClick={() => setParametersPanelField("url")}
              onChange={(event) => {
                handleChange("url", event.target.value);
              }}
              value={inputs.url}
              placeholder={placeholders["navigation"]["url"]}
              className="nopan text-xs"
            />
          </div>
          <div className="space-y-2">
            <div className="flex gap-2">
              <Label className="text-xs text-slate-300">Navigation Goal</Label>
              <HelpTooltip
                content={helpTooltips["navigation"]["navigationGoal"]}
              />
            </div>
            <WorkflowBlockInputTextarea
              onIconClick={() => setParametersPanelField("navigationGoal")}
              onChange={(event) => {
                handleChange("navigationGoal", event.target.value);
              }}
              value={inputs.navigationGoal}
              placeholder={placeholders["navigation"]["navigationGoal"]}
              className="nopan text-xs"
            />
          </div>
          <div className="rounded-md bg-slate-800 p-2">
            <div className="space-y-1 text-xs text-slate-400">
              Tip: While executing the navigation block, Skyvern will take
              multiple actions to accomplish a single goal.
            </div>
          </div>
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
                      content={helpTooltips["navigation"]["maxRetries"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={placeholders["navigation"]["maxRetries"]}
                    className="nopan w-52 text-xs"
                    min="0"
                    value={inputs.maxRetries ?? ""}
                    onChange={(event) => {
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
                      content={helpTooltips["navigation"]["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={placeholders["navigation"]["maxStepsOverride"]}
                    className="nopan w-52 text-xs"
                    min="0"
                    value={inputs.maxStepsOverride ?? ""}
                    onChange={(event) => {
                      const value =
                        event.target.value === ""
                          ? null
                          : Number(event.target.value);
                      handleChange("maxStepsOverride", value);
                    }}
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-4">
                    <div className="flex gap-2">
                      <Label className="text-xs font-normal text-slate-300">
                        Error Messages
                      </Label>
                      <HelpTooltip
                        content={helpTooltips["navigation"]["errorCodeMapping"]}
                      />
                    </div>
                    <Checkbox
                      checked={inputs.errorCodeMapping !== "null"}
                      disabled={!editable}
                      onCheckedChange={(checked) => {
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
                      content={helpTooltips["navigation"]["continueOnFailure"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.continueOnFailure}
                      onCheckedChange={(checked) => {
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
                      content={helpTooltips["navigation"]["cacheActions"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.cacheActions}
                      onCheckedChange={(checked) => {
                        handleChange("cacheActions", checked);
                      }}
                    />
                  </div>
                </div>
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Complete on Download
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["navigation"]["completeOnDownload"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.allowDownloads}
                      onCheckedChange={(checked) => {
                        handleChange("allowDownloads", checked);
                      }}
                    />
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      File Suffix
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["navigation"]["fileSuffix"]}
                    />
                  </div>
                  <WorkflowBlockInput
                    onIconClick={() => {
                      setParametersPanelField("downloadSuffix");
                    }}
                    type="text"
                    placeholder={placeholders["navigation"]["downloadSuffix"]}
                    className="nopan w-52 text-xs"
                    value={inputs.downloadSuffix ?? ""}
                    onChange={(event) => {
                      handleChange("downloadSuffix", event.target.value);
                    }}
                  />
                </div>
                <Separator />
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Verification URL
                    </Label>
                    <HelpTooltip
                      content={
                        helpTooltips["navigation"]["totpVerificationUrl"]
                      }
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    onIconClick={() => {
                      setParametersPanelField("totpVerificationUrl");
                    }}
                    onChange={(event) => {
                      handleChange("totpVerificationUrl", event.target.value);
                    }}
                    value={inputs.totpVerificationUrl ?? ""}
                    placeholder={
                      placeholders["navigation"]["totpVerificationUrl"]
                    }
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Identifier
                    </Label>
                    <HelpTooltip
                      content={helpTooltips["navigation"]["totpIdentifier"]}
                    />
                  </div>
                  <WorkflowBlockInputTextarea
                    onIconClick={() => {
                      setParametersPanelField("totpIdentifier");
                    }}
                    onChange={(event) => {
                      handleChange("totpIdentifier", event.target.value);
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
      {typeof parametersPanelField === "string" && (
        <WorkflowBlockParameterSelect
          nodeId={id}
          onClose={() => setParametersPanelField(null)}
          onAdd={(parameterKey) => {
            if (parametersPanelField === null || !editable) {
              return;
            }
            if (parametersPanelField in inputs) {
              const currentValue =
                inputs[parametersPanelField as keyof typeof inputs];
              handleChange(
                parametersPanelField,
                `${currentValue ?? ""}{{ ${parameterKey} }}`,
              );
            }
          }}
        />
      )}
    </div>
  );
}

export { NavigationNode };
