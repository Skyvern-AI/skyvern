import { ExclamationTriangleIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Label } from "@/components/ui/label";
import { HelpTooltip } from "@/components/HelpTooltip";
import { statusIsFinalized } from "@/routes/tasks/types";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { useCacheKeyValuesQuery } from "@/routes/workflows/hooks/useCacheKeyValuesQuery";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { constructCacheKeyValue } from "@/routes/workflows/editor/utils";
import { getCode, getOrderedBlockLabels } from "@/routes/workflows/utils";
import { useUiStore } from "@/store/UiStore";

interface Props {
  showCacheKeyValueSelector?: boolean;
}

function WorkflowRunCode(props?: Props) {
  const showCacheKeyValueSelector = props?.showCacheKeyValueSelector ?? false;
  const queryClient = useQueryClient();
  const { workflowPermanentId } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: workflow } = useWorkflowQuery({
    workflowPermanentId,
  });
  const cacheKey = workflow?.cache_key ?? "";
  const [cacheKeyValue, setCacheKeyValue] = useState(
    cacheKey === ""
      ? ""
      : constructCacheKeyValue({ codeKey: cacheKey, workflow, workflowRun }),
  );
  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    debounceMs: 100,
    page: 1,
    workflowPermanentId,
  });

  const isFinalized = workflowRun ? statusIsFinalized(workflowRun) : null;
  const parameters = workflowRun?.parameters;

  const { data: blockScripts } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    pollIntervalMs: !isFinalized ? 3000 : undefined,
    status: isFinalized ? "published" : "pending",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  const orderedBlockLabels = getOrderedBlockLabels(workflow);
  const code = getCode(orderedBlockLabels, blockScripts).join("").trim();
  const isGeneratingCode = !isFinalized && workflow?.generate_script === true;
  const couldBeGeneratingCode =
    !isFinalized && workflow?.generate_script !== true;

  const { setHighlightGenerateCodeToggle } = useUiStore();

  useEffect(() => {
    setCacheKeyValue(
      constructCacheKeyValue({ codeKey: cacheKey, workflow, workflowRun }) ??
        cacheKeyValues?.values[0],
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKeyValues, parameters, setCacheKeyValue, workflow, workflowRun]);

  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: [
        "cache-key-values",
        workflowPermanentId,
        cacheKey,
        1,
        undefined,
      ],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient, workflow]);

  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: [
        "block-scripts",
        workflowPermanentId,
        cacheKey,
        cacheKeyValue,
        undefined,
        "pending",
        workflowRun?.workflow_run_id,
      ],
    });
  }, [queryClient, workflowRun, workflowPermanentId, cacheKey, cacheKeyValue]);

  if (code.length === 0 && isFinalized) {
    return (
      <div className="flex items-center justify-center bg-slate-elevation3 p-8">
        No code has been generated yet.
      </div>
    );
  }

  const cacheKeyValueSet = new Set([...(cacheKeyValues?.values ?? [])]);

  const cacheKeyValueForWorkflowRun = constructCacheKeyValue({
    codeKey: cacheKey,
    workflow,
    workflowRun,
  });

  if (cacheKeyValueForWorkflowRun) {
    cacheKeyValueSet.add(cacheKeyValueForWorkflowRun);
  }

  return (
    <div className="flex h-full w-full flex-col items-end justify-start gap-2">
      {isGeneratingCode && (
        <div className="mb-6 flex w-full gap-2 rounded-md border-[1px] border-[slate-300] p-2">
          <div className="p6 flex w-full items-center justify-center rounded-l-md bg-slate-elevation5 px-4 py-2 text-sm">
            Generating code...
          </div>
          <div className="p6 flex items-center justify-center rounded-r-md bg-slate-elevation5 px-4 py-2 text-sm">
            <ReloadIcon className="size-8 animate-spin" />
          </div>
        </div>
      )}
      {couldBeGeneratingCode && (
        <div className="mb-6 flex w-full gap-2 rounded-md border-[1px] border-[slate-300] p-2">
          <div className="flex w-full items-center justify-center gap-2 rounded-l-md text-sm">
            <div className="flex-1 bg-slate-elevation5 p-4">
              Code generation disabled for this run. Please enable{" "}
              <a
                className="underline hover:text-sky-500"
                href={`${location.origin}/workflows/${workflowPermanentId}/debug`}
                target="_blank"
                onClick={() => setHighlightGenerateCodeToggle(true)}
              >
                Generate Code
              </a>{" "}
              in your Workflow Settings to have Skyvern generate code.
            </div>
          </div>
          <div className="p6 flex items-center justify-center rounded-r-md bg-slate-elevation5 px-4 py-2 text-sm">
            <ExclamationTriangleIcon className="size-8 text-[gold]" />
          </div>
        </div>
      )}
      {showCacheKeyValueSelector && cacheKey && cacheKey !== "" && (
        <div className="flex w-full justify-end gap-4">
          <div className="flex items-center justify-around gap-2">
            <Label className="w-[7rem]">Code Key Value</Label>
            <HelpTooltip
              content={
                !isFinalized
                  ? "The code key value the generated code is being stored under."
                  : "Which generated (& cached) code to view."
              }
            />
          </div>
          <Select
            disabled={!isFinalized}
            value={cacheKeyValue}
            onValueChange={(v: string) => setCacheKeyValue(v)}
          >
            <SelectTrigger className="max-w-[15rem] [&>span]:text-ellipsis">
              <SelectValue placeholder="Code Key Value" />
            </SelectTrigger>
            <SelectContent>
              {Array.from(cacheKeyValueSet)
                .sort()
                .map((value) => {
                  return (
                    <SelectItem key={value} value={value}>
                      {value === cacheKeyValueForWorkflowRun &&
                      isFinalized === true ? (
                        <span className="underline">{value}</span>
                      ) : (
                        value
                      )}
                    </SelectItem>
                  );
                })}
            </SelectContent>
          </Select>
        </div>
      )}
      {(isGeneratingCode || (code && code.length > 0)) && (
        <CodeEditor
          className="h-full w-full overflow-y-scroll"
          language="python"
          value={code}
          lineWrap={false}
          readOnly
          fontSize={10}
        />
      )}
    </div>
  );
}

export { WorkflowRunCode };
