import { ExitIcon } from "@radix-ui/react-icons";
import { useParams } from "react-router-dom";
import { Handle } from "@xyflow/react";
import { Position } from "@xyflow/react";

import { KeyIcon } from "@/components/icons/KeyIcon";
import { WorkflowBlockType } from "@/routes/workflows/types/workflowTypes";
import { useToggleScriptForNodeCallback } from "@/routes/workflows/hooks/useToggleScriptForNodeCallback";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { cn } from "@/util/utils";

import { CodeEditor } from "./CodeEditor";
import { workflowBlockTitle } from "../editor/nodes/types";
import { WorkflowBlockIcon } from "../editor/nodes/WorkflowBlockIcon";
import { constructCacheKeyValue } from "../editor/utils";

function BlockCodeEditor({
  blockLabel,
  blockType,
  script,
  onClick,
}: {
  blockLabel: string;
  blockType: WorkflowBlockType;
  script: string | undefined;
  onClick?: (e: React.MouseEvent) => void;
}) {
  const { workflowPermanentId } = useParams();
  const blockTitle = workflowBlockTitle[blockType];
  const toggleScriptForNodeCallback = useToggleScriptForNodeCallback();

  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const cacheKey = workflow?.cache_key ?? "";
  const cacheKeyValue = workflow
    ? cacheKey === ""
      ? ""
      : constructCacheKeyValue(cacheKey, workflow)
    : "";

  return (
    <div className="h-full">
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

      <div
        className={cn(
          "transform-origin-center flex h-full w-[30rem] flex-col space-y-4 rounded-lg border border-slate-600 bg-slate-elevation3 px-6 py-4 transition-all",
        )}
        onClick={(e) => {
          onClick?.(e);
        }}
      >
        <header className="relative !mt-0 flex h-[2.75rem] justify-between gap-2">
          <div className="flex w-full gap-2">
            <div className="relative flex h-[2.75rem] w-[2.75rem] items-center justify-center overflow-hidden rounded border border-slate-600">
              <WorkflowBlockIcon
                workflowBlockType={blockType}
                className="size-6"
              />
              <div className="absolute -left-3 top-8 flex h-4 w-16 origin-top-left -rotate-45 transform items-center justify-center bg-yellow-400">
                <span className="text-xs font-bold text-black">code</span>
              </div>
            </div>
            <div className="flex w-full flex-col gap-1">
              {blockLabel}
              <div className="flex w-full items-center justify-center gap-1">
                <span className="text-xs text-slate-400">{blockTitle}</span>
                <div className="ml-auto scale-[60%] opacity-50">
                  <KeyIcon />
                </div>
                <span className="text-xs text-slate-400">
                  {cacheKeyValue === "" ? "(none)" : cacheKeyValue}
                </span>
              </div>
            </div>
          </div>
          <div className="absolute right-[-0.5rem] top-0 flex h-[2rem] w-[2rem] items-center justify-center rounded hover:bg-slate-800">
            <ExitIcon
              onClick={() => {
                toggleScriptForNodeCallback({
                  label: blockLabel,
                  show: false,
                });
              }}
              className="size-5 cursor-pointer"
            />
          </div>
        </header>
        {script ? (
          <div className="h-full flex-1 overflow-y-hidden">
            <CodeEditor
              key="static"
              className="nopan nowheel h-full overflow-y-scroll"
              language="python"
              value={script}
              lineWrap={false}
              readOnly
              fontSize={10}
            />
          </div>
        ) : (
          <div className="flex h-full w-full items-center justify-center bg-slate-950">
            No script defined
          </div>
        )}
      </div>
    </div>
  );
}

export { BlockCodeEditor };
