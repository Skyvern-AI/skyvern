import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CalendarIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  CodeIcon,
  CopyIcon,
  CounterClockwiseClockIcon,
  DotsHorizontalIcon,
  PlayIcon,
  ReloadIcon,
  ResetIcon,
} from "@radix-ui/react-icons";
import { useMemo } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { VersionHistoryIcon } from "@/components/icons/VersionHistoryIcon";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { toast } from "@/components/ui/use-toast";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";
import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { convert } from "./workflowEditorUtils";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { isMacPlatform } from "@/util/platform";
import { cn } from "@/util/utils";
import { CacheKeyValuesResponse } from "@/routes/workflows/types/scriptTypes";

type Props = {
  cacheKeyValue: string | null;
  cacheKeyValues: CacheKeyValuesResponse | undefined;
  canUndo: boolean;
  canRedo: boolean;
  isGeneratingCode?: boolean;
  isTemplate?: boolean;
  parametersPanelOpen: boolean;
  saving: boolean;
  showAllCode: boolean;
  onCacheKeyValueAccept: (cacheKeyValue: string | null) => void;
  onBrowseCacheKeys?: () => void;
  onParametersClick: () => void;
  onScheduleClick: () => void;
  onShowAllCodeClick?: () => void;
  onSave: () => void;
  onUndo: () => void;
  onRedo: () => void;
  onRun?: () => void;
  onHistory?: () => void;
};

function WorkflowHeader({
  cacheKeyValue,
  cacheKeyValues,
  canUndo,
  canRedo,
  isGeneratingCode,
  isTemplate,
  parametersPanelOpen,
  saving,
  showAllCode,
  onCacheKeyValueAccept,
  onBrowseCacheKeys,
  onParametersClick,
  onScheduleClick,
  onShowAllCodeClick,
  onSave,
  onUndo,
  onRedo,
  onRun,
  onHistory,
}: Readonly<Props>) {
  const { title, setTitle } = useWorkflowTitleStore();
  const workflowChangesStore = useWorkflowHasChangesStore();
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const { data: workflowRun } = useWorkflowRunQuery();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);

  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { undoShortcutLabel, redoShortcutLabel } = useMemo(() => {
    const mac = isMacPlatform();
    return {
      undoShortcutLabel: mac ? "⌘Z" : "Ctrl+Z",
      redoShortcutLabel: mac ? "⌘⇧Z" : "Ctrl+Shift+Z",
    };
  }, []);

  const templateMutation = useMutation({
    mutationFn: async (newIsTemplate: boolean) => {
      // Template endpoint only exists on /v1 (no /api prefix)
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.put(
        `/workflows/${workflowPermanentId}/template?is_template=${newIsTemplate}`,
      );
    },
    onSuccess: (_, newIsTemplate) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["orgTemplates"],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflow", workflowPermanentId],
      });
      toast({
        title: newIsTemplate ? "Saved as template" : "Removed from templates",
        variant: "success",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to update template status",
        description: error.message,
      });
    },
  });

  const handleShowAllCode = () => {
    onShowAllCodeClick?.();
  };

  const isRecording = recordingStore.isRecording;

  const shouldShowCacheControls =
    !isRecording && !isGeneratingCode && (cacheKeyValues?.total_count ?? 0) > 0;

  if (!globalWorkflows) {
    return null; // this should be loaded already by some other components
  }

  const isGlobalWorkflow = globalWorkflows.some(
    (workflow) => workflow.workflow_permanent_id === workflowPermanentId,
  );

  return (
    <div
      className={cn(
        "flex h-full w-full items-center justify-between rounded-xl bg-slate-elevation2 px-4 py-5 xl:px-6",
      )}
    >
      <div className="mr-2 flex h-full min-w-0 flex-1 items-center xl:mr-4">
        <EditableNodeTitle
          editable={!isRecording}
          onChange={(newTitle) => {
            setTitle(newTitle);
            workflowChangesStore.setHasChanges(true);
          }}
          value={title}
          titleClassName="text-2xl xl:text-3xl"
          inputClassName="text-2xl xl:text-3xl"
        />
      </div>
      <div className="flex h-full shrink-0 items-center justify-end gap-2 xl:gap-4">
        {isGeneratingCode && (
          <Button
            className="size-10 min-w-[6rem]"
            variant={!showAllCode ? "tertiary" : "default"}
            onClick={handleShowAllCode}
          >
            <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            Code
          </Button>
        )}
        {isGlobalWorkflow ? (
          <Button
            size="lg"
            onClick={() => {
              const workflow = globalWorkflows.find(
                (workflow) =>
                  workflow.workflow_permanent_id === workflowPermanentId,
              );
              if (!workflow) {
                return; // makes no sense
              }
              const clone = convert(workflow);
              createWorkflowMutation.mutate(clone);
            }}
          >
            {createWorkflowMutation.isPending ? (
              <ReloadIcon className="mr-3 h-6 w-6 animate-spin" />
            ) : (
              <CopyIcon className="mr-3 h-6 w-6" />
            )}
            Make a Copy to Edit
          </Button>
        ) : (
          <>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant={debugStore.isDebugMode ? "default" : "tertiary"}
                    className="size-10 min-w-[2.5rem]"
                    disabled={workflowRunIsRunningOrQueued || isRecording}
                    onClick={() => {
                      if (debugStore.isDebugMode) {
                        navigate(`/workflows/${workflowPermanentId}/edit`);
                      } else {
                        navigate(`/workflows/${workflowPermanentId}/build`);
                      }
                    }}
                  >
                    {debugStore.isDebugMode ? (
                      <BrowserIcon className="h-6 w-6" />
                    ) : (
                      <BrowserIcon className="h-6 w-6" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {debugStore.isDebugMode
                    ? "Turn off Browser"
                    : "Turn on Browser"}
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="tertiary"
                    className="size-10 min-w-[2.5rem]"
                    disabled={!canUndo || isRecording}
                    onClick={onUndo}
                    aria-label="Undo"
                  >
                    <ResetIcon className="size-6" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Undo ({undoShortcutLabel})</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="tertiary"
                    className="size-10 min-w-[2.5rem]"
                    disabled={!canRedo || isRecording}
                    onClick={onRedo}
                    aria-label="Redo"
                  >
                    <ResetIcon className="size-6 -scale-x-100" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Redo ({redoShortcutLabel})</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="tertiary"
                    className="size-10 min-w-[2.5rem]"
                    disabled={isGlobalWorkflow || isRecording}
                    onClick={() => {
                      onSave();
                    }}
                  >
                    {saving ? (
                      <ReloadIcon className="size-6 animate-spin" />
                    ) : (
                      <SaveIcon className="size-6" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Save</TooltipContent>
              </Tooltip>
            </TooltipProvider>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  size="icon"
                  variant="tertiary"
                  className="size-10 min-w-[2.5rem]"
                  disabled={isRecording}
                  aria-label="More actions"
                >
                  <DotsHorizontalIcon className="size-6" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                {shouldShowCacheControls && (
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger>
                      <CodeIcon className="mr-2 size-4" />
                      Cache key: {cacheKeyValue || "default"}
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="max-h-72 overflow-y-auto">
                      <DropdownMenuRadioGroup
                        value={cacheKeyValue ?? ""}
                        onValueChange={(v) => onCacheKeyValueAccept(v || null)}
                      >
                        <DropdownMenuRadioItem value="">
                          Default (no cache key)
                        </DropdownMenuRadioItem>
                        {cacheKeyValues?.values?.map((value) => (
                          <DropdownMenuRadioItem key={value} value={value}>
                            {value}
                          </DropdownMenuRadioItem>
                        ))}
                      </DropdownMenuRadioGroup>
                      {(cacheKeyValues?.values?.length ?? 0) === 0 && (
                        <div className="px-2 py-1.5 text-xs text-slate-400">
                          No cache keys yet
                        </div>
                      )}
                      {onBrowseCacheKeys && (
                        <>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem onSelect={onBrowseCacheKeys}>
                            Browse all cache keys…
                          </DropdownMenuItem>
                        </>
                      )}
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                )}
                {shouldShowCacheControls && debugStore.isDebugMode && (
                  <DropdownMenuItem onSelect={handleShowAllCode}>
                    <CodeIcon className="mr-2 size-4" />
                    {showAllCode ? "Hide Code" : "Show Code"}
                  </DropdownMenuItem>
                )}
                {shouldShowCacheControls && <DropdownMenuSeparator />}
                <DropdownMenuItem
                  disabled={isRecording || templateMutation.isPending || saving}
                  onSelect={() => {
                    const newIsTemplate = !isTemplate;
                    if (newIsTemplate) {
                      onSave();
                    }
                    templateMutation.mutate(newIsTemplate);
                  }}
                >
                  {templateMutation.isPending ? (
                    <ReloadIcon className="mr-2 size-4 animate-spin" />
                  ) : isTemplate ? (
                    <BookmarkFilledIcon className="mr-2 size-4" />
                  ) : (
                    <BookmarkIcon className="mr-2 size-4" />
                  )}
                  {templateMutation.isPending
                    ? "Saving…"
                    : isTemplate
                      ? "Remove from Templates"
                      : "Save as Template"}
                </DropdownMenuItem>
                {!workflowRunIsRunningOrQueued && (
                  <DropdownMenuItem
                    disabled={isRecording}
                    onSelect={() => {
                      onHistory?.();
                    }}
                  >
                    <VersionHistoryIcon size={16} className="mr-2" />
                    History
                  </DropdownMenuItem>
                )}
                <DropdownMenuItem
                  disabled={isRecording}
                  onSelect={onScheduleClick}
                >
                  <CalendarIcon className="mr-2 size-4" />
                  Schedule…
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => {
                    navigate(`/workflows/${workflowPermanentId}/runs`);
                  }}
                >
                  <CounterClockwiseClockIcon className="mr-2 size-4" />
                  Run history
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              disabled={isRecording}
              variant="tertiary"
              size="lg"
              onClick={onParametersClick}
            >
              <span className="mr-2">Parameters</span>
              {parametersPanelOpen ? (
                <ChevronUpIcon className="h-6 w-6" />
              ) : (
                <ChevronDownIcon className="h-6 w-6" />
              )}
            </Button>
            <Button
              disabled={isRecording}
              size="lg"
              onClick={() => {
                onRun?.();
                navigate(`/workflows/${workflowPermanentId}/run`);
              }}
            >
              <PlayIcon className="mr-2 h-6 w-6" />
              Run
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

export { WorkflowHeader };
