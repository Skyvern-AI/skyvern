import {
  CheckIcon,
  ChevronDownIcon,
  ChevronUpIcon,
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
import { useUser } from "@/hooks/useUser";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";
import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { convert } from "./workflowEditorUtils";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useDebugStore } from "@/store/useDebugStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { cn } from "@/util/utils";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { CacheKeyValuesResponse } from "@/routes/workflows/types/scriptTypes";
import { OrgWalled } from "@/components/Orgwalled";

function CopyButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    if (copied) {
      return;
    }
    window.navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <Button size="icon" variant="ghost" onClick={handleCopy}>
      {copied ? <CheckIcon /> : <CopyIcon />}
    </Button>
  );
}

interface Dom {
  input: React.MutableRefObject<HTMLInputElement | null>;
}

type Props = {
  cacheKeyValue: string | null;
  cacheKeyValues: CacheKeyValuesResponse | undefined;
  cacheKeyValuesPanelOpen: boolean;
  parametersPanelOpen: boolean;
  saving: boolean;
  workflow: WorkflowApiResponse;
  onCacheKeyValueAccept: (cacheKeyValue: string | null) => void;
  onCacheKeyValuesBlurred: (cacheKeyValue: string | null) => void;
  onCacheKeyValuesFilter: (cacheKeyValue: string) => void;
  onCacheKeyValuesKeydown: (e: React.KeyboardEvent<HTMLInputElement>) => void;
  onParametersClick: () => void;
  onCacheKeyValuesClick: () => void;
  onSave: () => void;
  onRun?: () => void;
};

function WorkflowHeader({
  cacheKeyValue,
  cacheKeyValues,
  cacheKeyValuesPanelOpen,
  parametersPanelOpen,
  saving,
  workflow,
  onCacheKeyValueAccept,
  onCacheKeyValuesBlurred,
  onCacheKeyValuesFilter,
  onCacheKeyValuesKeydown,
  onParametersClick,
  onCacheKeyValuesClick,
  onSave,
  onRun,
}: Props) {
  const { title, setTitle } = useWorkflowTitleStore();
  const workflowChangesStore = useWorkflowHasChangesStore();
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const { data: workflowRun } = useWorkflowRunQuery();
  const debugStore = useDebugStore();
  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);
  const user = useUser().get();
  const [chosenCacheKeyValue, setChosenCacheKeyValue] = useState<string | null>(
    cacheKeyValue ?? null,
  );

  const dom: Dom = {
    input: useRef<HTMLInputElement>(null),
  };

  useEffect(() => {
    if (cacheKeyValue === chosenCacheKeyValue) {
      return;
    }

    setChosenCacheKeyValue(cacheKeyValue ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKeyValue]);

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
          editable={true}
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
        {user && workflow.generate_script && (
          // (cacheKeyValues?.total_count ?? 0) > 0 && (
          <OrgWalled className="p-0">
            <div
              tabIndex={1}
              className="flex max-w-[15rem] items-center justify-center gap-1 rounded-md border border-input pr-1 focus-within:ring-1 focus-within:ring-ring"
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
                placeholder="Script Key Value"
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
              <CopyButton value={chosenCacheKeyValue ?? ""} />
            </div>
          </OrgWalled>
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
            {user && (
              <TooltipProvider>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button
                      size="icon"
                      variant={debugStore.isDebugMode ? "default" : "tertiary"}
                      className="size-10 min-w-[2.5rem]"
                      disabled={workflowRunIsRunningOrQueued}
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
            )}
            <TooltipProvider>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    size="icon"
                    variant="tertiary"
                    className="size-10 min-w-[2.5rem]"
                    disabled={isGlobalWorkflow}
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
            <Button variant="tertiary" size="lg" onClick={onParametersClick}>
              <span className="mr-2">Parameters</span>
              {parametersPanelOpen ? (
                <ChevronUpIcon className="h-6 w-6" />
              ) : (
                <ChevronDownIcon className="h-6 w-6" />
              )}
            </Button>
            <Button
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
