import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
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
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useToast } from "@/components/ui/use-toast";
import { Pencil1Icon } from "@radix-ui/react-icons";

import { CopyAndExplainCode } from "../editor/Workspace";
import { ScriptFixInput } from "./ScriptFixInput";

const enableCodeBlock =
  import.meta.env.VITE_ENABLE_CODE_BLOCK?.toLowerCase() === "true";

interface Props {
  showCacheKeyValueSelector?: boolean;
}

function WorkflowRunCode(props?: Props) {
  const showCacheKeyValueSelector = props?.showCacheKeyValueSelector ?? false;
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const { toast } = useToast();
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

  // Reset selected version and exit edit mode when the active script changes
  useEffect(() => {
    setSelectedVersion(null);
    setIsEditing(false);
    setEditedCode("");
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

  // --- Edit mode state ---
  const [isEditing, setIsEditing] = useState(false);
  const [editedCode, setEditedCode] = useState("");
  const originalCodeRef = useRef("");

  const canEdit =
    scriptId && code.length > 0 && !isGeneratingCode && !isViewingOtherVersion;

  const handleStartEditing = useCallback(() => {
    originalCodeRef.current = code;
    setEditedCode(code);
    setIsEditing(true);
  }, [code]);

  const handleCancelEditing = useCallback(() => {
    setIsEditing(false);
    setEditedCode("");
    originalCodeRef.current = "";
  }, []);

  const deployMutation = useMutation({
    mutationFn: async ({
      scriptId,
      mainPyContent,
    }: {
      scriptId: string;
      mainPyContent: string;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const bytes = new TextEncoder().encode(mainPyContent);
      const encoded = btoa(
        Array.from(bytes, (b) => String.fromCharCode(b)).join(""),
      );
      return client.post(`/scripts/${scriptId}/deploy`, {
        files: [
          {
            path: "main.py",
            content: encoded,
            encoding: "base64",
            mime_type: "text/x-python",
          },
        ],
      });
    },
    onSuccess: (response) => {
      setIsEditing(false);
      setEditedCode("");
      // Auto-select the newly created version so the editor doesn't revert
      if (response.data.version != null) {
        setSelectedVersion(response.data.version);
      }
      toast({
        title: "Script saved",
        description: `Version ${response.data.version} created.`,
      });
      // Invalidate script queries so the new version shows up
      queryClient.invalidateQueries({ queryKey: ["block-scripts"] });
      queryClient.invalidateQueries({ queryKey: ["script-versions"] });
    },
    onError: () => {
      toast({
        variant: "destructive",
        title: "Failed to save",
        description: "Could not save the script. Please try again.",
      });
    },
  });

  const handleSave = useCallback(() => {
    if (!scriptId) return;
    // Prevent saving empty scripts
    if (editedCode.trim() === "") {
      toast({
        variant: "destructive",
        title: "Cannot save empty script",
        description: "The script must contain code.",
      });
      return;
    }
    // No changes — just exit edit mode
    if (editedCode === originalCodeRef.current) {
      setIsEditing(false);
      setEditedCode("");
      return;
    }
    deployMutation.mutate({ scriptId, mainPyContent: editedCode });
  }, [scriptId, editedCode, deployMutation, toast]);

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

  // After a successful AI fix, auto-select the new version so the user
  // immediately sees the updated code.
  const handleScriptUpdated = useCallback(
    (data: { version: number }) => {
      setSelectedVersion(data.version);
    },
    [setSelectedVersion],
  );

  if (code.length === 0 && !isGeneratingCode) {
    return (
      <div className="flex items-center justify-center bg-slate-elevation3 p-8">
        No code has been generated yet.
      </div>
    );
  }

  const hasVersions = versions.length > 1;

  // Edit button shown when not in edit mode and there's a script to edit
  const editButton =
    enableCodeBlock && canEdit && !isEditing ? (
      <Button
        variant="outline"
        size="sm"
        className="h-7 gap-1.5 px-2.5 text-xs"
        onClick={handleStartEditing}
      >
        <Pencil1Icon className="size-3" />
        Edit
      </Button>
    ) : null;

  // Save / Cancel buttons shown during edit mode
  const editActions =
    enableCodeBlock && isEditing ? (
      <div className="flex items-center gap-2">
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2.5 text-xs"
          onClick={handleCancelEditing}
          disabled={deployMutation.isPending}
        >
          Cancel
        </Button>
        <Button
          size="sm"
          className="h-7 px-2.5 text-xs"
          onClick={handleSave}
          disabled={deployMutation.isPending}
        >
          {deployMutation.isPending ? "Saving..." : "Save"}
        </Button>
      </div>
    ) : null;

  // Version selector — badge when single version, dropdown when multiple
  const versionSelector = !versionsFetched ? null : hasVersions ? (
    <Select
      value={String(selectedVersion ?? currentVersion ?? "")}
      onValueChange={(v: string) => setSelectedVersion(Number(v))}
      disabled={isEditing}
    >
      <SelectTrigger className="h-7 w-auto min-w-[5rem] gap-1.5 rounded-full border-slate-700 px-2.5 text-xs">
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
  ) : currentVersion != null ? (
    <span className="rounded-full border border-slate-700 px-2.5 py-1 text-xs text-slate-400">
      v{currentVersion}
    </span>
  ) : null;

  if (!showCacheKeyValueSelector || !cacheKey || cacheKey === "") {
    return (
      <div className="flex h-full w-full flex-col gap-2">
        <div className="flex items-center justify-end gap-2">
          {editButton}
          {editActions}
          {versionSelector}
          {!isEditing && code.length > 0 && <CopyAndExplainCode code={code} />}
        </div>
        {enableCodeBlock &&
          code.length > 0 &&
          isFinalized &&
          workflowPermanentId && (
            <ScriptFixInput
              workflowPermanentId={workflowPermanentId}
              workflowRunId={workflowRun?.workflow_run_id}
              onScriptUpdated={handleScriptUpdated}
            />
          )}
        <CodeEditor
          className={cn("h-full overflow-y-scroll", {
            "animate-pulse": isGeneratingCode || isLoadingVersion,
          })}
          language="python"
          value={isEditing ? editedCode : code}
          onChange={isEditing ? setEditedCode : undefined}
          lineWrap={false}
          readOnly={!isEditing}
          fontSize={10}
        />
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
    <div className="flex h-full w-full flex-col items-end justify-center gap-2">
      <div className="flex w-full items-center justify-end gap-2">
        {editButton}
        {editActions}
        {versionSelector}
        {cacheKeyValueSet.size > 0 ? (
          <div className="flex items-center gap-1.5">
            <HelpTooltip
              content={
                !isFinalized
                  ? "The cached variant the generated code is stored under."
                  : "Which cached code variant to view."
              }
            />
            <Select
              disabled={!isFinalized || isEditing}
              value={cacheKeyValue}
              onValueChange={(v: string) => setCacheKeyValue(v)}
            >
              <SelectTrigger className="h-7 max-w-[15rem] gap-1.5 rounded-full border-slate-700 px-2.5 text-xs [&>span]:text-ellipsis">
                <SelectValue placeholder="Variant" />
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
        {!isEditing && code.length > 0 && <CopyAndExplainCode code={code} />}
      </div>
      {enableCodeBlock &&
        code.length > 0 &&
        isFinalized &&
        workflowPermanentId && (
          <ScriptFixInput
            workflowPermanentId={workflowPermanentId}
            workflowRunId={workflowRun?.workflow_run_id}
            onScriptUpdated={handleScriptUpdated}
          />
        )}
      <CodeEditor
        className={cn("h-full w-full overflow-y-scroll", {
          "animate-pulse": isGeneratingCode || isLoadingVersion,
        })}
        language="python"
        value={isEditing ? editedCode : code}
        onChange={isEditing ? setEditedCode : undefined}
        lineWrap={false}
        readOnly={!isEditing}
        fontSize={10}
      />
    </div>
  );
}

export { WorkflowRunCode };
