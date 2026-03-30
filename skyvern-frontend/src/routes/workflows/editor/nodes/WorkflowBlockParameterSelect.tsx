import { useEdges, useNodes } from "@xyflow/react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode } from ".";
import {
  getAvailableOutputParameterKeys,
  isNodeInsideForLoop,
} from "../workflowEditorUtils";
import {
  GLOBAL_RESERVED_PARAMETERS,
  LOOP_RESERVED_PARAMETERS,
} from "../constants";
import { PlusIcon } from "@radix-ui/react-icons";
import { SwitchBar } from "@/components/SwitchBar";
import { useState } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ScrollAreaViewport } from "@radix-ui/react-scroll-area";

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
