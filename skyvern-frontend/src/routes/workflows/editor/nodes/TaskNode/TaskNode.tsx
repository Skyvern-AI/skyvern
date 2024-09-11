import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Switch } from "@/components/ui/switch";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { ListBulletIcon, MixerVerticalIcon } from "@radix-ui/react-icons";
import { Handle, NodeProps, Position, useReactFlow } from "@xyflow/react";
import { useState } from "react";
import { EditableNodeTitle } from "../components/EditableNodeTitle";
import { NodeActionMenu } from "../NodeActionMenu";
import { TaskNodeDisplayModeSwitch } from "./TaskNodeDisplayModeSwitch";
import { TaskNodeParametersPanel } from "./TaskNodeParametersPanel";
import type { TaskNode, TaskNodeDisplayMode } from "./types";
import { useDeleteNodeCallback } from "@/routes/workflows/hooks/useDeleteNodeCallback";

function TaskNode({ id, data }: NodeProps<TaskNode>) {
  const { updateNodeData } = useReactFlow();
  const [displayMode, setDisplayMode] = useState<TaskNodeDisplayMode>("basic");
  const { editable } = data;
  const deleteNodeCallback = useDeleteNodeCallback();

  const [label, setLabel] = useState(data.label);
  const [inputs, setInputs] = useState({
    url: data.url,
    navigationGoal: data.navigationGoal,
    dataExtractionGoal: data.dataExtractionGoal,
    dataSchema: data.dataSchema,
    maxRetries: data.maxRetries,
    maxStepsOverride: data.maxStepsOverride,
    allowDownloads: data.allowDownloads,
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

  const basicContent = (
    <>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">URL</Label>
        <AutoResizingTextarea
          value={inputs.url}
          className="nopan"
          name="url"
          onChange={(event) => {
            if (!editable) {
              return;
            }
            handleChange("url", event.target.value);
          }}
          placeholder="https://"
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">Goal</Label>
        <AutoResizingTextarea
          onChange={(event) => {
            if (!editable) {
              return;
            }
            handleChange("navigationGoal", event.target.value);
          }}
          value={inputs.navigationGoal}
          placeholder="What are you looking to do?"
          className="nopan"
        />
      </div>
    </>
  );

  const advancedContent = (
    <>
      <Accordion
        type="multiple"
        defaultValue={["content", "extraction", "limits", "totp"]}
      >
        <AccordionItem value="content">
          <AccordionTrigger>Content</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">URL</Label>
                <AutoResizingTextarea
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("url", event.target.value);
                  }}
                  value={inputs.url}
                  placeholder="https://"
                  className="nopan"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Goal</Label>
                <AutoResizingTextarea
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("navigationGoal", event.target.value);
                  }}
                  value={inputs.navigationGoal}
                  placeholder="What are you looking to do?"
                  className="nopan"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="extraction">
          <AccordionTrigger>Extraction</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  Data Extraction Goal
                </Label>
                <AutoResizingTextarea
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("dataExtractionGoal", event.target.value);
                  }}
                  value={inputs.dataExtractionGoal}
                  placeholder="What outputs are you looking to get?"
                  className="nopan"
                />
              </div>
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs text-slate-300">Data Schema</Label>
                  <Checkbox
                    checked={inputs.dataSchema !== "null"}
                    onCheckedChange={(checked) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("dataSchema", checked ? "{}" : "null");
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
                    />
                  </div>
                )}
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="limits">
          <AccordionTrigger>Limits</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1 pt-1">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Max Retries
                </Label>
                <Input
                  type="number"
                  placeholder="0"
                  className="nopan w-44"
                  min="0"
                  value={inputs.maxRetries ?? 0}
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("maxRetries", Number(event.target.value));
                  }}
                />
              </div>
              <div className="flex items-center justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Max Steps Override
                </Label>
                <Input
                  type="number"
                  placeholder="0"
                  className="nopan w-44"
                  min="0"
                  value={inputs.maxStepsOverride ?? 0}
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange(
                      "maxStepsOverride",
                      Number(event.target.value),
                    );
                  }}
                />
              </div>
              <div className="flex justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Allow Downloads
                </Label>
                <div className="w-44">
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
              <div className="space-y-2">
                <div className="flex gap-2">
                  <Label className="text-xs font-normal text-slate-300">
                    Error Messages
                  </Label>
                  <Checkbox
                    checked={inputs.errorCodeMapping !== "null"}
                    disabled={!editable}
                    onCheckedChange={(checked) => {
                      if (!editable) {
                        return;
                      }
                      handleChange("errorCodeMapping", checked ? "{}" : "null");
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
                    />
                  </div>
                )}
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="totp">
          <AccordionTrigger>TOTP</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  TOTP Verification URL
                </Label>
                <AutoResizingTextarea
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("totpVerificationUrl", event.target.value);
                  }}
                  value={inputs.totpVerificationUrl ?? ""}
                  placeholder="https://"
                  className="nopan"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">
                  TOTP Identifier
                </Label>
                <AutoResizingTextarea
                  onChange={(event) => {
                    if (!editable) {
                      return;
                    }
                    handleChange("totpIdentifier", event.target.value);
                  }}
                  value={inputs.totpIdentifier ?? ""}
                  placeholder="Identifier"
                  className="nopan"
                />
              </div>
            </div>
          </AccordionContent>
        </AccordionItem>
      </Accordion>
    </>
  );

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
              <ListBulletIcon className="h-6 w-6" />
            </div>
            <div className="flex flex-col gap-1">
              <EditableNodeTitle
                value={label}
                editable={editable}
                onChange={(value) => {
                  setLabel(value);
                  updateNodeData(id, { label: value });
                }}
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
        <div className="flex justify-between">
          <TaskNodeDisplayModeSwitch
            value={displayMode}
            onChange={setDisplayMode}
          />
          <Popover>
            <PopoverTrigger asChild>
              <Button size="icon" variant="outline">
                <MixerVerticalIcon />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-72">
              <TaskNodeParametersPanel
                parameters={data.parameterKeys}
                onParametersChange={(parameterKeys) => {
                  updateNodeData(id, { parameterKeys });
                }}
              />
            </PopoverContent>
          </Popover>
        </div>

        {displayMode === "basic" && basicContent}
        {displayMode === "advanced" && advancedContent}
      </div>
    </div>
  );
}

export { TaskNode };
