import { useEffect, useState } from "react";
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
import { useScriptVersionsQuery } from "@/routes/workflows/hooks/useScriptVersionsQuery";
import { useScriptVersionCodeQuery } from "@/routes/workflows/hooks/useScriptVersionCodeQuery";
import { useWorkflowRunWithWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery";
import { constructCacheKeyValue } from "@/routes/workflows/editor/utils";
import { getCode, getOrderedBlockLabels } from "@/routes/workflows/utils";
import { cn } from "@/util/utils";

import { CopyAndExplainCode } from "../editor/Workspace";

interface Props {
  showCacheKeyValueSelector?: boolean;
}

function WorkflowRunCode(props?: Props) {
  const showCacheKeyValueSelector = props?.showCacheKeyValueSelector ?? false;
  const queryClient = useQueryClient();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  const workflow = workflowRun?.workflow;
  const workflowPermanentId = workflow?.workflow_permanent_id;
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

  const isAdaptiveCaching =
    workflow?.adaptive_caching && workflow?.run_with === "code";

  useEffect(() => {
    const keys = Object.keys(blockScriptsPublished?.blocks ?? {});
    setHasPublishedCode(
      keys.length > 0 || Boolean(blockScriptsPublished?.main_script),
    );
  }, [blockScriptsPublished, setHasPublishedCode]);

  const orderedBlockLabels = getOrderedBlockLabels(workflow);

  const activeScripts = hasPublishedCode
    ? blockScriptsPublished
    : blockScriptsPending;

  // Script version state
  const scriptId = activeScripts?.script_id ?? null;
  const currentVersion = activeScripts?.version ?? null;
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);

  // Reset selected version when the active script changes
  useEffect(() => {
    setSelectedVersion(null);
  }, [scriptId, currentVersion]);

  // Fetch available versions for this script
  const { data: versionsData, isFetched: versionsFetched } =
    useScriptVersionsQuery({ scriptId });
  // Sort descending so versions[0] is always the latest
  const versions = [...(versionsData?.versions ?? [])].sort(
    (a, b) => b.version - a.version,
  );

  // If user selected a different version, fetch that version's code
  const isViewingOtherVersion =
    selectedVersion !== null && selectedVersion !== currentVersion;
  const { data: selectedVersionCode, isFetching: isLoadingVersion } =
    useScriptVersionCodeQuery({
      scriptId,
      version: isViewingOtherVersion ? selectedVersion : null,
    });

  // Determine which code to display
  const displayScripts = isViewingOtherVersion
    ? selectedVersionCode
    : activeScripts;

  // For non-adaptive-caching, use block labels from the displayed version
  // (older versions may have different blocks than the current run)
  const displayBlockLabels = isViewingOtherVersion
    ? Object.keys(displayScripts?.blocks ?? {})
    : orderedBlockLabels;

  // For adaptive caching, prefer the full main.py script over stitched blocks
  const code = (
    isAdaptiveCaching && displayScripts?.main_script
      ? displayScripts.main_script
      : getCode(displayBlockLabels, displayScripts?.blocks).join("")
  ).trim();

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

  const hasVersions = versions.length > 1;

  // Version selector component (shared between both render paths)
  // Wait for the versions query to settle before rendering to avoid a flash
  // from static label to dropdown when the query resolves.
  const versionSelector = !versionsFetched ? null : hasVersions ? (
    <div className="flex items-center gap-2">
      <Label className="whitespace-nowrap text-xs text-slate-400">
        Version
      </Label>
      <Select
        value={String(selectedVersion ?? currentVersion ?? "")}
        onValueChange={(v: string) => setSelectedVersion(Number(v))}
      >
        <SelectTrigger className="h-7 w-auto min-w-[7rem] gap-1.5 px-2 text-xs">
          <SelectValue placeholder="Version" />
        </SelectTrigger>
        <SelectContent>
          {versions.map((v) => {
            const isRunVersion = v.version === currentVersion;
            const isLatest = v.version === versions[0]?.version;
            return (
              <SelectItem key={v.version} value={String(v.version)}>
                <span className="flex items-center gap-1.5">
                  <span
                    className={cn({
                      "font-semibold text-emerald-400": isRunVersion,
                    })}
                  >
                    v{v.version}
                  </span>
                  {isRunVersion && (
                    <span className="rounded-sm bg-emerald-900/50 px-1 py-0.5 text-[10px] leading-none text-emerald-300">
                      this run
                    </span>
                  )}
                  {isLatest && !isRunVersion && (
                    <span className="rounded-sm bg-blue-900/50 px-1 py-0.5 text-[10px] leading-none text-blue-300">
                      latest
                    </span>
                  )}
                </span>
              </SelectItem>
            );
          })}
        </SelectContent>
      </Select>
    </div>
  ) : currentVersion != null ? (
    <span className="text-xs text-slate-500">v{currentVersion}</span>
  ) : null;

  if (!showCacheKeyValueSelector || !cacheKey || cacheKey === "") {
    return (
      <div className="relative flex h-full w-full flex-col gap-2">
        {versionSelector && (
          <div className="flex justify-end">{versionSelector}</div>
        )}
        <CodeEditor
          className={cn("h-full overflow-y-scroll", {
            "animate-pulse": isGeneratingCode || isLoadingVersion,
          })}
          language="python"
          value={code}
          lineWrap={false}
          readOnly
          fontSize={10}
        />
        {code.length > 0 && (
          <div className="absolute bottom-2 right-3 flex items-center justify-end">
            <CopyAndExplainCode code={code} />
          </div>
        )}
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
    <div className="relative flex h-full w-full flex-col items-end justify-center gap-2">
      <div className="flex w-full items-center justify-end gap-4">
        {versionSelector}
        {cacheKeyValueSet.size > 0 ? (
          <div className="flex items-center gap-2">
            <Label className="w-[7rem]">Code Key Value</Label>
            <HelpTooltip
              content={
                !isFinalized
                  ? "The code key value the generated code is being stored under."
                  : "Which generated (& cached) code to view."
              }
            />
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
                  .map((value, i) => {
                    const v = value
                      ? value.length === 0
                        ? "default"
                        : value
                      : "default";

                    return (
                      <SelectItem key={`${v}-${i}`} value={v}>
                        {value === cacheKeyValueForWorkflowRun &&
                        isFinalized === true ? (
                          <span className="underline">{v}</span>
                        ) : (
                          v
                        )}
                      </SelectItem>
                    );
                  })}
              </SelectContent>
            </Select>
          </div>
        ) : null}
      </div>
      <CodeEditor
        className={cn("h-full w-full overflow-y-scroll", {
          "animate-pulse": isGeneratingCode || isLoadingVersion,
        })}
        language="python"
        value={code}
        lineWrap={false}
        readOnly
        fontSize={10}
      />
      <div className="absolute bottom-2 right-3 flex items-center justify-end">
        <CopyAndExplainCode code={code} />
      </div>
    </div>
  );
}

export { WorkflowRunCode };
