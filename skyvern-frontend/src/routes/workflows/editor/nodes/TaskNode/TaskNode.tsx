import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { HelpTooltip } from "@/components/HelpTooltip";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";
import { useNodeLabelChangeHandler } from "@/routes/workflows/hooks/useLabelChangeHandler";
import { ListBulletIcon } from "@radix-ui/react-icons";
import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useReactFlow,
} from "@xyflow/react";
import { useState } from "react";
import { AppNode } from "..";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { ParametersMultiSelect } from "./ParametersMultiSelect";
import { fieldPlaceholders, helpTooltipContent, type TaskNode } from "./types";
import { Separator } from "@/components/ui/separator";
import { dataSchemaExampleValue, errorMappingExampleValue } from "../types";

function TaskNode({ id, data }: NodeProps<TaskNode>) {
  const { updateNodeData } = useReactFlow();
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(nodes, edges, id);
  const [label, setLabel] = useNodeLabelChangeHandler({
    id,
    initialValue: data.label,
  });

  const [inputs, setInputs] = useState({
    url: data.url,
    navigationGoal: data.navigationGoal,
    dataExtractionGoal: data.dataExtractionGoal,
    dataSchema: data.dataSchema,
    maxRetries: data.maxRetries,
    maxStepsOverride: data.maxStepsOverride,
    allowDownloads: data.allowDownloads,
    continueOnFailure: data.continueOnFailure,
    cacheActions: data.cacheActions,
    downloadSuffix: data.downloadSuffix,
    errorCodeMapping: data.errorCodeMapping,
    totpVerificationUrl: data.totpVerificationUrl,
    totpIdentifier: data.totpIdentifier,
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
      <div className="w-[30rem] space-y-2 rounded-lg bg-slate-elevation3 px-6 py-4">
        <div className="flex h-[2.75rem] justify-between">
          <div className="flex gap-2">
            <div className="flex h-[2.75rem] w-[2.75rem] items-center justify-center rounded border border-slate-600">
              <ListBulletIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={setLabel}
                titleClassName="text-base"
                inputClassName="text-base"
              />
              <span className="text-xs text-slate-400">Task Block</span>
            </div>
          </div>
          <NodeActionMenu
            onDelete={() => {
              deleteNodeCallback(id);
            }}
          />
        </div>
        <Accordion type="multiple" defaultValue={["content", "extraction"]}>
          <AccordionItem value="content">
            <AccordionTrigger>Content</AccordionTrigger>
            <AccordionContent className="pl-[1.5rem] pr-1">
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">URL</Label>
                    <HelpTooltip content={helpTooltipContent["url"]} />
                  </div>
                  <AutoResizingTextarea
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("url", event.target.value);
                    }}
                    value={inputs.url}
                    placeholder={fieldPlaceholders["url"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">Goal</Label>
                    <HelpTooltip
                      content={helpTooltipContent["navigationGoal"]}
                    />
                  </div>
                  <AutoResizingTextarea
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("navigationGoal", event.target.value);
                    }}
                    value={inputs.navigationGoal}
                    placeholder={fieldPlaceholders["navigationGoal"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <ParametersMultiSelect
                    availableOutputParameters={outputParameterKeys}
                    parameters={data.parameterKeys}
                    onParametersChange={(parameterKeys) => {
                      updateNodeData(id, { parameterKeys });
                    }}
                  />
                </div>
              </div>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="extraction">
            <AccordionTrigger>Extraction</AccordionTrigger>
            <AccordionContent className="pl-[1.5rem] pr-1">
              <div className="space-y-4">
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      Data Extraction Goal
                    </Label>
                    <HelpTooltip
                      content={helpTooltipContent["dataExtractionGoal"]}
                    />
                  </div>
                  <AutoResizingTextarea
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("dataExtractionGoal", event.target.value);
                    }}
                    value={inputs.dataExtractionGoal}
                    placeholder={fieldPlaceholders["dataExtractionGoal"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-4">
                    <div className="flex gap-2">
                      <Label className="text-xs text-slate-300">
                        Data Schema
                      </Label>
                      <HelpTooltip content={helpTooltipContent["dataSchema"]} />
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
              </div>
            </AccordionContent>
          </AccordionItem>
          <AccordionItem value="advanced" className="border-b-0">
            <AccordionTrigger>Advanced Settings</AccordionTrigger>
            <AccordionContent className="pl-6 pr-1 pt-1">
              <div className="space-y-4">
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Max Retries
                    </Label>
                    <HelpTooltip content={helpTooltipContent["maxRetries"]} />
                  </div>
                  <Input
                    type="number"
                    placeholder={fieldPlaceholders["maxRetries"]}
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
                      content={helpTooltipContent["maxStepsOverride"]}
                    />
                  </div>
                  <Input
                    type="number"
                    placeholder={fieldPlaceholders["maxStepsOverride"]}
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
                    <HelpTooltip content={helpTooltipContent["cacheActions"]} />
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
                <Separator />
                <div className="flex items-center justify-between">
                  <div className="flex gap-2">
                    <Label className="text-xs font-normal text-slate-300">
                      Complete on Download
                    </Label>
                    <HelpTooltip
                      content={helpTooltipContent["completeOnDownload"]}
                    />
                  </div>
                  <div className="w-52">
                    <Switch
                      checked={inputs.allowDownloads}
                      onCheckedChange={(checked) => {
                        if (!editable) {
                          return;
                        }
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
                    <HelpTooltip content={helpTooltipContent["fileSuffix"]} />
                  </div>
                  <Input
                    type="text"
                    placeholder={fieldPlaceholders["downloadSuffix"]}
                    className="nopan w-52 text-xs"
                    value={inputs.downloadSuffix ?? ""}
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
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
                      content={helpTooltipContent["totpVerificationUrl"]}
                    />
                  </div>
                  <AutoResizingTextarea
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("totpVerificationUrl", event.target.value);
                    }}
                    value={inputs.totpVerificationUrl ?? ""}
                    placeholder={fieldPlaceholders["totpVerificationUrl"]}
                    className="nopan text-xs"
                  />
                </div>
                <div className="space-y-2">
                  <div className="flex gap-2">
                    <Label className="text-xs text-slate-300">
                      2FA Identifier
                    </Label>
                    <HelpTooltip
                      content={helpTooltipContent["totpIdentifier"]}
                    />
                  </div>
                  <AutoResizingTextarea
                    onChange={(event) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("totpIdentifier", event.target.value);
                    }}
                    value={inputs.totpIdentifier ?? ""}
                    placeholder={fieldPlaceholders["totpIdentifier"]}
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

export { TaskNode };
