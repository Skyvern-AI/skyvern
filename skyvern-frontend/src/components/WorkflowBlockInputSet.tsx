import { PlusIcon } from "@radix-ui/react-icons";
import { Popover, PopoverContent, PopoverTrigger } from "./ui/popover";
import { WorkflowBlockParameterSelect } from "@/routes/workflows/editor/nodes/WorkflowBlockParameterSelect";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { useState, useEffect } from "react";
import { Cross2Icon } from "@radix-ui/react-icons";
import "./workflow-block-input-set.css";
type Props = {
  onChange: (parameterKeys: Set<string>) => void;
  nodeId: string;
  values: Set<string>;
};

function WorkflowBlockInputSet(props: Props) {
  const { nodeId, onChange, values } = props;
  const [parameterKeys, setParameterKeys] = useState<Set<string>>(values);
  const hasKeys = parameterKeys.size > 0;
  const { parameters: workflowParameters } = useWorkflowParametersStore();
  const availableParameterKeys = new Set(
    workflowParameters.map((parameter) => parameter.key),
  );

  useEffect(() => {
    onChange(new Set(parameterKeys));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [parameterKeys]);

  return (
    <div className="workflow-block-input-set relative rounded-md border border-input">
      <div className="ze-set">
        {hasKeys ? (
          Array.from(parameterKeys).map((parameterKey) => {
            const missing = !availableParameterKeys.has(parameterKey);

            return (
              <div
                key={parameterKey}
                className={`parameter-key flex items-center gap-2 ${missing ? "missing" : ""}`}
              >
                <span>{parameterKey}</span>
                <Cross2Icon
                  className="size-4 cursor-pointer"
                  onClick={() => {
                    setParameterKeys((prev) => {
                      const newSet = new Set(prev);
                      newSet.delete(parameterKey);
                      return newSet;
                    });
                  }}
                />
              </div>
            );
          })
        ) : (
          <span className="flex items-center gap-2 text-slate-400">&nbsp;</span>
        )}
      </div>
      <div className="absolute right-0 top-0 flex size-9 cursor-pointer items-center justify-center">
        <Popover>
          <PopoverTrigger asChild>
            <div className="rounded p-1 hover:bg-muted">
              <PlusIcon className="size-4" />
            </div>
          </PopoverTrigger>
          <PopoverContent className="w-[22rem]">
            <WorkflowBlockParameterSelect
              nodeId={nodeId}
              onAdd={(parameterKey) => {
                setParameterKeys((prev) => {
                  const newSet = new Set(prev);
                  newSet.add(parameterKey);
                  return newSet;
                });
              }}
            />
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}

export { WorkflowBlockInputSet };
