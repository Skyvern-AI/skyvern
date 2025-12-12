import {
  BookmarkFilledIcon,
  BookmarkIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  ClockIcon,
  CodeIcon,
  CopyIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { Input } from "@/components/ui/input";
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
import { cn } from "@/util/utils";
import { CacheKeyValuesResponse } from "@/routes/workflows/types/scriptTypes";

interface Dom {
  input: React.MutableRefObject<HTMLInputElement | null>;
}

type Props = {
  cacheKeyValue: string | null;
  cacheKeyValues: CacheKeyValuesResponse | undefined;
  cacheKeyValuesPanelOpen: boolean;
  isGeneratingCode?: boolean;
  isTemplate?: boolean;
  parametersPanelOpen: boolean;
  saving: boolean;
  showAllCode: boolean;
  onCacheKeyValueAccept: (cacheKeyValue: string | null) => void;
  onCacheKeyValuesBlurred: (cacheKeyValue: string | null) => void;
  onCacheKeyValuesFilter: (cacheKeyValue: string) => void;
  onCacheKeyValuesKeydown: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onParametersClick: () => void;
  onShowAllCodeClick?: () => void;
  onCacheKeyValuesClick: () => void;
  onSave: () => void;
  onRun?: () => void;
  onHistory?: () => void;
};

function WorkflowHeader({
  cacheKeyValue,
  cacheKeyValues,
  cacheKeyValuesPanelOpen,
  isGeneratingCode,
  isTemplate,
  parametersPanelOpen,
  saving,
  showAllCode,
  onCacheKeyValueAccept,
  onCacheKeyValuesBlurred,
  onCacheKeyValuesFilter,
  onCacheKeyValuesKeydown,
  onParametersClick,
  onShowAllCodeClick,
  onCacheKeyValuesClick,
  onSave,
  onRun,
  onHistory,
}: Props) {
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
  const [chosenCacheKeyValue, setChosenCacheKeyValue] = useState<string | null>(
    cacheKeyValue ?? null,
  );

  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

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

  const dom: Dom = {
    input: useRef<HTMLInputElement>(null),
  };

  const handleShowAllCode = () => {
    onShowAllCodeClick?.();
  };

  useEffect(() => {
    if (cacheKeyValue === chosenCacheKeyValue) {
      return;
    }

    setChosenCacheKeyValue(cacheKeyValue ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKeyValue]);

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
        "flex h-full w-full justify-between rounded-xl bg-slate-elevation2 px-6 py-5",
      )}
    >
      <div className="flex h-full items-center">
        <EditableNodeTitle
          editable={!isRecording}
          onChange={(newTitle) => {
            setTitle(newTitle);
            workflowChangesStore.setHasChanges(true);
          }}
          value={title}
          titleClassName="text-3xl"
          inputClassName="text-3xl"
        />
      </div>
      <div className="flex h-full items-center justify-end gap-4">
        {shouldShowCacheControls && (
          <>
            {debugStore.isDebugMode && (
              <Button
                className="pl-2 pr-3"
                size="lg"
                variant={!showAllCode ? "tertiary" : "default"}
                onClick={handleShowAllCode}
              >
                <CodeIcon className="mr-2 h-6 w-6" />
                Show Code
              </Button>
            )}
            <div
              tabIndex={1}
              className="flex max-w-[10rem] items-center justify-center gap-1 rounded-md border border-input pr-1 focus-within:ring-1 focus-within:ring-ring"
            >
              <Input
                ref={dom.input}
                className="focus-visible:transparent focus-visible:none h-[2.75rem] text-ellipsis whitespace-nowrap border-none focus-visible:outline-none focus-visible:ring-0"
                onChange={(e) => {
                  setChosenCacheKeyValue(e.target.value);
                  onCacheKeyValuesFilter(e.target.value);
                }}
                onMouseDown={() => {
                  if (!cacheKeyValuesPanelOpen) {
                    onCacheKeyValuesClick();
                  }
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    const numFiltered = cacheKeyValues?.values?.length ?? 0;

                    if (numFiltered === 1) {
                      const first = cacheKeyValues?.values?.[0];
                      if (first) {
                        setChosenCacheKeyValue(first);
                        onCacheKeyValueAccept(first);
                      }
                      return;
                    }

                    setChosenCacheKeyValue(chosenCacheKeyValue);
                    onCacheKeyValueAccept(chosenCacheKeyValue);
                  }
                  onCacheKeyValuesKeydown(e);
                }}
                placeholder="Code Key Value"
                value={chosenCacheKeyValue ?? undefined}
                onBlur={(e) => {
                  onCacheKeyValuesBlurred(e.target.value);
                  setChosenCacheKeyValue(e.target.value);
                }}
              />
              {cacheKeyValuesPanelOpen ? (
                <ChevronUpIcon
                  className="h-6 w-6 cursor-pointer"
                  onClick={onCacheKeyValuesClick}
                />
              ) : (
                <ChevronDownIcon
                  className="h-6 w-6 cursor-pointer"
                  onClick={() => {
                    dom.input.current?.focus();
                    onCacheKeyValuesClick();
                  }}
                />
              )}
            </div>
          </>
        )}
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
                        navigate(`/workflows/${workflowPermanentId}/debug`);
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
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    disabled={
                      isRecording || templateMutation.isPending || saving
                    }
                    size="icon"
                    variant={isTemplate ? "default" : "tertiary"}
                    className="size-10 min-w-[2.5rem]"
                    onClick={() => {
                      const newIsTemplate = !isTemplate;
                      if (newIsTemplate) {
                        // When saving AS template, save the workflow first
                        onSave();
                      }
                      templateMutation.mutate(newIsTemplate);
                    }}
                  >
                    {templateMutation.isPending ? (
                      <ReloadIcon className="size-6 animate-spin" />
                    ) : isTemplate ? (
                      <BookmarkFilledIcon className="size-6" />
                    ) : (
                      <BookmarkIcon className="size-6" />
                    )}
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {isTemplate ? "Remove from Templates" : "Save as Template"}
                </TooltipContent>
              </Tooltip>
            </TooltipProvider>
            {!workflowRunIsRunningOrQueued && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      disabled={isRecording}
                      size="icon"
                      variant="tertiary"
                      className="size-10 min-w-[2.5rem]"
                      onClick={() => {
                        onHistory?.();
                      }}
                    >
                      <ClockIcon className="size-6" />
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>History</TooltipContent>
                </Tooltip>
              </TooltipProvider>
            )}
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
