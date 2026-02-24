import { useEdges, useNodes } from "@xyflow/react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode } from ".";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "../workflowEditorUtils";
import { PlusIcon } from "@radix-ui/react-icons";
import { SwitchBar } from "@/components/SwitchBar";
import { useState } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ScrollAreaViewport } from "@radix-ui/react-scroll-area";

// Reserved parameters that are always available
// See: skyvern/forge/sdk/workflow/models/parameter.py RESERVED_PARAMETER_KEYS
const GLOBAL_RESERVED_PARAMETERS = [
  { key: "current_date", description: "Current UTC date (YYYY-MM-DD format)" },
  {
    key: "workflow_run_outputs",
    description: "JSON of all block outputs collected so far",
  },
  {
    key: "workflow_run_summary",
    description: "Merged summary of all block outputs",
  },
  { key: "workflow_run_id", description: "Unique ID of the current run" },
  { key: "workflow_id", description: "The workflow's ID" },
  {
    key: "workflow_permanent_id",
    description: "The workflow's permanent ID",
  },
  { key: "workflow_title", description: "The workflow's title" },
];

// Reserved parameters only available inside loop blocks
const LOOP_RESERVED_PARAMETERS = [
  { key: "current_value", description: "The current item being iterated" },
  { key: "current_item", description: "Alias for current_value" },
  {
    key: "current_index",
    description: "Zero-based index of the current iteration",
  },
];

type Props = {
  nodeId: string;
  onAdd: (parameterKey: string) => void;
};

function WorkflowBlockParameterSelect({ nodeId, onAdd }: Props) {
  const [content, setContent] = useState("parameters");
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    nodeId,
  );
  const allParameterKeys = workflowParameters.map((parameter) => parameter.key);

  const insideLoop = isNodeInsideForLoop(nodes, nodeId);
  const reservedParameters = [
    ...GLOBAL_RESERVED_PARAMETERS,
    ...(insideLoop ? LOOP_RESERVED_PARAMETERS : []),
  ];

  return (
    <div className="cursor-auto space-y-3">
      <header className="flex justify-between">
        <h1>Add Parameter</h1>
      </header>
      <SwitchBar
        className="w-full"
        onChange={(value) => setContent(value)}
        value={content}
        options={[
          {
            label: "Parameters",
            value: "parameters",
          },
          {
            label: "Block Outputs",
            value: "outputs",
          },
          {
            label: "System",
            value: "system",
          },
        ]}
      />
      <ScrollArea>
        <ScrollAreaViewport className="max-h-96">
          {content === "parameters" && (
            <div className="space-y-2">
              {allParameterKeys.map((parameterKey) => {
                return (
                  <div
                    key={parameterKey}
                    className="flex cursor-pointer justify-between rounded-md bg-slate-elevation1 px-3 py-2 text-xs hover:bg-slate-elevation2"
                    onClick={() => {
                      onAdd(parameterKey);
                    }}
                  >
                    {parameterKey}
                    <PlusIcon />
                  </div>
                );
              })}
              {allParameterKeys.length === 0 && (
                <div className="text-xs">No parameters</div>
              )}
            </div>
          )}
          {content === "outputs" && (
            <div className="space-y-2">
              {outputParameterKeys.map((parameterKey) => {
                return (
                  <div
                    key={parameterKey}
                    className="flex cursor-pointer justify-between rounded-md bg-slate-elevation1 px-3 py-2 text-xs hover:bg-slate-elevation2"
                    onClick={() => {
                      onAdd?.(parameterKey);
                    }}
                  >
                    {parameterKey}
                    <PlusIcon />
                  </div>
                );
              })}
              {outputParameterKeys.length === 0 && (
                <div className="text-xs">No output parameters</div>
              )}
            </div>
          )}
          {content === "system" && (
            <div className="space-y-2">
              {reservedParameters.map(({ key, description }) => (
                <div
                  key={key}
                  className="flex cursor-pointer items-center justify-between rounded-md bg-slate-elevation1 px-3 py-2 text-xs hover:bg-slate-elevation2"
                  onClick={() => {
                    onAdd(key);
                  }}
                >
                  <div>
                    <div>{key}</div>
                    <div className="text-[0.625rem] text-muted-foreground">
                      {description}
                    </div>
                  </div>
                  <PlusIcon className="shrink-0" />
                </div>
              ))}
              {reservedParameters.length === 0 && (
                <div className="text-xs">No reserved parameters</div>
              )}
            </div>
          )}
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowBlockParameterSelect };
