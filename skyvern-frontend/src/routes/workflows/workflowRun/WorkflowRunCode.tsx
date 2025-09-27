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
import { cn } from "@/util/utils";

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

  const [hasPublishedCode, setHasPublishedCode] = useState(false);

  const { data: blockScriptsPending } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    pollIntervalMs: !hasPublishedCode && !isFinalized ? 3000 : undefined,
    status: "pending",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  const { data: blockScriptsPublished } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    status: "published",
    workflowRunId: workflowRun?.workflow_run_id,
  });

  useEffect(() => {
    const keys = Object.keys(blockScriptsPublished ?? {});
    setHasPublishedCode(keys.length > 0);
  }, [blockScriptsPublished, setHasPublishedCode]);

  const orderedBlockLabels = getOrderedBlockLabels(workflow);

  const code = getCode(
    orderedBlockLabels,
    hasPublishedCode ? blockScriptsPublished : blockScriptsPending,
  )
    .join("")
    .trim();

  const isGeneratingCode = !isFinalized && !hasPublishedCode;

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

  if (code.length === 0 && !isGeneratingCode) {
    return (
      <div className="flex items-center justify-center bg-slate-elevation3 p-8">
        No code has been generated yet.
      </div>
    );
  }

  if (!showCacheKeyValueSelector || !cacheKey || cacheKey === "") {
    return (
      <CodeEditor
        className="h-full overflow-y-scroll"
        language="python"
        value={code}
        lineWrap={false}
        readOnly
        fontSize={10}
      />
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
    <div className="flex h-full w-full flex-col items-end justify-center gap-2">
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
      <CodeEditor
        className={cn("h-full w-full overflow-y-scroll", {
          "animate-pulse": isGeneratingCode,
        })}
        language="python"
        value={code}
        lineWrap={false}
        readOnly
        fontSize={10}
      />
    </div>
  );
}

export { WorkflowRunCode };
