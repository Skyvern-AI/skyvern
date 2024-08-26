import { Handle, NodeProps, Position } from "@xyflow/react";
import { useState } from "react";
import { DotsHorizontalIcon, ListBulletIcon } from "@radix-ui/react-icons";
import { TaskNodeDisplayModeSwitch } from "./TaskNodeDisplayModeSwitch";
import type { TaskNodeDisplayMode } from "./types";
import type { TaskNode } from "./types";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Label } from "@/components/ui/label";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { DataSchema } from "../../../components/DataSchema";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { TaskNodeErrorMapping } from "./TaskNodeErrorMapping";

function TaskNode({ data }: NodeProps<TaskNode>) {
  const [displayMode, setDisplayMode] = useState<TaskNodeDisplayMode>("basic");
  const { editable } = data;

  const basicContent = (
    <>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">URL</Label>
        <AutoResizingTextarea
          value={data.url}
          className="nopan"
          onChange={() => {
            if (!editable) return;
            // TODO
          }}
          placeholder="https://"
        />
      </div>
      <div className="space-y-1">
        <Label className="text-xs text-slate-300">Goal</Label>
        <AutoResizingTextarea
          onChange={() => {
            if (!editable) return;
            // TODO
          }}
          value={data.navigationGoal}
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
        defaultValue={["content", "extraction", "limits"]}
      >
        <AccordionItem value="content">
          <AccordionTrigger>Content</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">URL</Label>
                <AutoResizingTextarea
                  onChange={() => {
                    if (!editable) return;
                    // TODO
                  }}
                  value={data.url}
                  placeholder="https://"
                  className="nopan"
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs text-slate-300">Goal</Label>
                <AutoResizingTextarea
                  onChange={() => {
                    if (!editable) return;
                    // TODO
                  }}
                  value={data.navigationGoal}
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
                  onChange={() => {
                    if (!editable) return;
                    // TODO
                  }}
                  value={data.dataExtractionGoal}
                  placeholder="What outputs are you looking to get?"
                  className="nopan"
                />
              </div>
              <DataSchema
                value={data.dataSchema}
                onChange={() => {
                  if (!editable) return;
                  // TODO
                }}
              />
            </div>
          </AccordionContent>
        </AccordionItem>
        <AccordionItem value="limits">
          <AccordionTrigger>Limits</AccordionTrigger>
          <AccordionContent className="pl-[1.5rem] pr-1">
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Max Retries
                </Label>
                <Input
                  type="number"
                  placeholder="0"
                  className="nopan w-44"
                  value={data.maxRetries ?? 0}
                  onChange={() => {
                    if (!editable) return;
                    // TODO
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
                  value={data.maxStepsOverride ?? 0}
                  onChange={() => {
                    if (!editable) return;
                    // TODO
                  }}
                />
              </div>
              <div className="flex justify-between">
                <Label className="text-xs font-normal text-slate-300">
                  Allow Downloads
                </Label>
                <div className="w-44">
                  <Switch
                    checked={data.allowDownloads}
                    onCheckedChange={() => {
                      if (!editable) return;
                      // TODO
                    }}
                  />
                </div>
              </div>
              <TaskNodeErrorMapping
                value={data.errorCodeMapping}
                onChange={() => {
                  if (!editable) return;
                  // TODO
                }}
              />
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
              <span className="max-w-64 truncate text-base">{data.label}</span>
              <span className="text-xs text-slate-400">Task Block</span>
            </div>
          </div>
          <div>
            <DotsHorizontalIcon className="h-6 w-6" />
          </div>
        </div>
        <TaskNodeDisplayModeSwitch
          value={displayMode}
          onChange={setDisplayMode}
        />
        {displayMode === "basic" && basicContent}
        {displayMode === "advanced" && advancedContent}
      </div>
    </div>
  );
}

export { TaskNode };
