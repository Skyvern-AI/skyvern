import { useEdges, useNodes } from "@xyflow/react";
import { useWorkflowParametersState } from "../useWorkflowParametersState";
import { AppNode } from ".";
import { getAvailableOutputParameterKeys } from "../workflowEditorUtils";
import { Cross2Icon, PlusIcon } from "@radix-ui/react-icons";
import { SwitchBar } from "@/components/SwitchBar";
import { useState } from "react";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ScrollAreaViewport } from "@radix-ui/react-scroll-area";

type Props = {
  nodeId: string;
  onClose: () => void;
  onAdd: (parameterKey: string) => void;
};

function WorkflowBlockParameterSelect({ nodeId, onClose, onAdd }: Props) {
  const [content, setContent] = useState("parameters");
  const [workflowParameters] = useWorkflowParametersState();
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const outputParameterKeys = getAvailableOutputParameterKeys(
    nodes,
    edges,
    nodeId,
  );
  const workflowParameterKeys = workflowParameters.map(
    (parameter) => parameter.key,
  );

  return (
    <div className="nopan nowheel absolute right-[-296px] top-0 mt-0 w-[280px] cursor-auto space-y-3 rounded-md border border-slate-700 bg-slate-950 p-4">
      <header className="flex justify-between">
        <h1>Add Parameter</h1>
        <Cross2Icon className="size-6 cursor-pointer" onClick={onClose} />
      </header>
      <SwitchBar
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
        ]}
      />
      <ScrollArea>
        <ScrollAreaViewport className="max-h-96">
          {content === "parameters" && (
            <div className="space-y-2">
              {workflowParameterKeys.map((parameterKey) => {
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
              {workflowParameterKeys.length === 0 && (
                <div className="text-xs">No workflow parameters</div>
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
                      onAdd(parameterKey);
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
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowBlockParameterSelect };
