import { useEdges, useNodes } from "@xyflow/react";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { AppNode } from ".";
import { getAvailableOutputParameterKeys } from "../workflowEditorUtils";
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
  const credentialParameters = workflowParameters.filter(
    (parameter) => parameter.parameterType === "credential",
  );
  const credentialParameterKeys = credentialParameters.map(
    (parameter) => parameter.key,
  );

  const nonCredentialParameters = workflowParameters.filter(
    (parameter) => parameter.parameterType !== "credential",
  );
  const nonCredentialParameterKeys = nonCredentialParameters.map(
    (parameter) => parameter.key,
  );

  return (
    <div className="cursor-auto space-y-3">
      <header className="flex justify-between">
        <h1>Add Parameter</h1>
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
            label: "Credentials",
            value: "credentials",
          },
          {
            label: "Block Outputs",
            value: "outputs",
          },
        ]}
      />
      <ScrollArea>
        <ScrollAreaViewport className="max-h-96">
          {content === "credentials" && (
            <div className="space-y-2">
              {credentialParameterKeys.map((parameterKey) => {
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
              {credentialParameterKeys.length === 0 && (
                <div className="text-xs">No credentials</div>
              )}
            </div>
          )}
          {content === "parameters" && (
            <div className="space-y-2">
              {nonCredentialParameterKeys.map((parameterKey) => {
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
              {nonCredentialParameterKeys.length === 0 && (
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
        </ScrollAreaViewport>
      </ScrollArea>
    </div>
  );
}

export { WorkflowBlockParameterSelect };
