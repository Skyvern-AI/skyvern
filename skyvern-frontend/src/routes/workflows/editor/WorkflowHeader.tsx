import {
  ChevronDownIcon,
  ChevronUpIcon,
  ClockIcon,
  CodeIcon,
  CopyIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { VersionHistoryIcon } from "@/components/icons/VersionHistoryIcon";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useCacheKeyValuesQuery } from "@/routes/workflows/hooks/useCacheKeyValuesQuery";
import { useCreateWorkflowMutation } from "../hooks/useCreateWorkflowMutation";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useDebugStore } from "@/store/useDebugStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useShowAllCodeStore } from "@/store/ShowAllCodeStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { cn } from "@/util/utils";
import { EditableNodeTitle } from "./nodes/components/EditableNodeTitle";
import { EditorOverflowMenu } from "./header/EditorOverflowMenu";
import { useIsGeneratingCode } from "./hooks/useIsGeneratingCode";
import { useSaveWorkflow } from "./hooks/useSaveWorkflow";
import { useToggleCodeView } from "./hooks/useToggleCodeView";
import { useToggleHistoryPanel } from "./hooks/useToggleHistoryPanel";
import { useWorkflowHeaderCollapseStore } from "./useWorkflowHeaderCollapseStore";
import { WorkflowHeaderCollapseTab } from "./WorkflowHeaderCollapseTab";
import { convert } from "./workflowEditorUtils";

function useIsGlobalWorkflow(): boolean {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  return Boolean(
    globalWorkflows?.some(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    ),
  );
}

function ShowCodeButton() {
  const showAllCode = useShowAllCodeStore((s) => s.showAllCode);
  const toggleCodeView = useToggleCodeView();

  return (
    <Button
      className="pl-2 pr-3"
      size="lg"
      variant={showAllCode ? "default" : "tertiary"}
      onClick={toggleCodeView}
    >
      <CodeIcon className="mr-2 h-6 w-6" />
      Show Code
    </Button>
  );
}

function CacheKeyValueDropdown() {
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const cacheKeyValueFilter = useCacheKeyValueStore((s) => s.filter);
  const setExplicitCacheKeyValue = useCacheKeyValueStore((s) => s.setExplicit);
  const setCacheKeyValueFilter = useCacheKeyValueStore((s) => s.setFilter);
  const workflowPanelState = useWorkflowPanelStore((s) => s.workflowPanelState);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );
  const closeWorkflowPanel = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const cacheKeyValuesPanelOpen =
    workflowPanelState.active &&
    workflowPanelState.content === "cacheKeyValues";

  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";
  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    debounceMs: 100,
    filter: cacheKeyValueFilter || undefined,
    page: 1,
    workflowPermanentId,
  });

  const [chosenCacheKeyValue, setChosenCacheKeyValue] = useState<string | null>(
    cacheKeyValue ?? null,
  );

  const inputRef = useRef<HTMLInputElement>(null);

  // Sync local input state when the external store value changes. The
  // functional setter form lets us read the current local value without
  // listing it as a dependency — including chosenCacheKeyValue would
  // refire the effect on every keystroke even though the user-typed value
  // shouldn't pull the store value back.
  useEffect(() => {
    setChosenCacheKeyValue((current) =>
      current === (cacheKeyValue ?? null) ? current : (cacheKeyValue ?? null),
    );
  }, [cacheKeyValue]);

  const openCacheKeyValuesPanel = () => {
    setWorkflowPanelState({ active: true, content: "cacheKeyValues" });
  };

  const acceptOnEnter = () => {
    const numFiltered = cacheKeyValues?.values?.length ?? 0;
    if (numFiltered === 1) {
      const first = cacheKeyValues?.values?.[0];
      if (first) {
        setChosenCacheKeyValue(first);
        setExplicitCacheKeyValue(first);
        setCacheKeyValueFilter(null);
        closeWorkflowPanel();
      }
      return;
    }
    setExplicitCacheKeyValue(chosenCacheKeyValue ?? "");
    setCacheKeyValueFilter(null);
    closeWorkflowPanel();
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      acceptOnEnter();
    }
    if (e.key !== "Tab") {
      openCacheKeyValuesPanel();
    }
  };

  return (
    <div className="flex max-w-[10rem] items-center justify-center gap-1 rounded-md border border-input pr-1 focus-within:ring-1 focus-within:ring-ring">
      <Input
        ref={inputRef}
        className="focus-visible:transparent focus-visible:none h-[2.75rem] text-ellipsis whitespace-nowrap border-none focus-visible:outline-none focus-visible:ring-0"
        onChange={(e) => {
          setChosenCacheKeyValue(e.target.value);
          setCacheKeyValueFilter(e.target.value);
        }}
        onMouseDown={() => {
          if (!cacheKeyValuesPanelOpen) {
            openCacheKeyValuesPanel();
          }
        }}
        onKeyDown={handleKeyDown}
        placeholder="Code Key Value"
        value={chosenCacheKeyValue ?? undefined}
        onBlur={(e) => {
          setExplicitCacheKeyValue(e.target.value);
          setChosenCacheKeyValue(e.target.value);
        }}
      />
      {cacheKeyValuesPanelOpen ? (
        <ChevronUpIcon
          className="h-6 w-6 cursor-pointer"
          onClick={() => closeWorkflowPanel()}
        />
      ) : (
        <ChevronDownIcon
          className="h-6 w-6 cursor-pointer"
          onClick={() => {
            inputRef.current?.focus();
            openCacheKeyValuesPanel();
          }}
        />
      )}
    </div>
  );
}

function CacheKeyValueControls() {
  const debugStore = useDebugStore();
  return (
    <>
      {debugStore.isDebugMode && <ShowCodeButton />}
      <CacheKeyValueDropdown />
    </>
  );
}

function GeneratingCodeButton() {
  const showAllCode = useShowAllCodeStore((s) => s.showAllCode);
  const toggleCodeView = useToggleCodeView();
  return (
    <Button
      className="size-10 min-w-[6rem]"
      variant={showAllCode ? "default" : "tertiary"}
      onClick={toggleCodeView}
    >
      <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
      Code
    </Button>
  );
}

function MakeACopyButton() {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const createWorkflowMutation = useCreateWorkflowMutation();

  const handleClick = () => {
    const workflow = globalWorkflows?.find(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    );
    if (!workflow) {
      return;
    }
    createWorkflowMutation.mutate(convert(workflow));
  };

  return (
    <Button size="lg" onClick={handleClick}>
      {createWorkflowMutation.isPending ? (
        <ReloadIcon className="mr-3 h-6 w-6 animate-spin" />
      ) : (
        <CopyIcon className="mr-3 h-6 w-6" />
      )}
      Make a Copy to Edit
    </Button>
  );
}

function BrowserModeButton() {
  const navigate = useNavigate();
  const { workflowPermanentId } = useParams();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued = Boolean(
    workflowRun && statusIsRunningOrQueued(workflowRun),
  );

  const handleClick = () => {
    const target = debugStore.isDebugMode ? "edit" : "build";
    navigate(`/workflows/${workflowPermanentId}/${target}`);
  };

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            size="icon"
            variant={debugStore.isDebugMode ? "default" : "tertiary"}
            className="size-10 min-w-[2.5rem]"
            disabled={
              workflowRunIsRunningOrQueued || recordingStore.isRecording
            }
            onClick={handleClick}
          >
            <BrowserIcon className="h-6 w-6" />
          </Button>
        </TooltipTrigger>
        <TooltipContent>
          {debugStore.isDebugMode ? "Turn off Browser" : "Turn on Browser"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function SaveButton() {
  const saving = useWorkflowHasChangesStore((s) => s.saveIsPending);
  const isRecording = useRecordingStore().isRecording;
  const isGlobalWorkflow = useIsGlobalWorkflow();
  const onSave = useSaveWorkflow();

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            size="icon"
            variant="tertiary"
            className="size-10 min-w-[2.5rem]"
            disabled={isGlobalWorkflow || isRecording}
            onClick={() => {
              void onSave();
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
  );
}

function HistoryButton() {
  const isRecording = useRecordingStore().isRecording;
  const toggleHistoryPanel = useToggleHistoryPanel();

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            disabled={isRecording}
            size="icon"
            variant="tertiary"
            className="size-10 min-w-[2.5rem]"
            onClick={() => {
              toggleHistoryPanel();
            }}
          >
            <VersionHistoryIcon size={24} />
          </Button>
        </TooltipTrigger>
        <TooltipContent>History</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

type PanelToggleContent = "schedules" | "parameters";

type PanelToggleButtonProps = {
  content: PanelToggleContent;
  label: string;
  leadingIcon?: ReactNode;
  iconOnly?: boolean;
};

function PanelToggleButton({
  content,
  label,
  leadingIcon,
  iconOnly = false,
}: PanelToggleButtonProps) {
  const isRecording = useRecordingStore().isRecording;
  const workflowPanelState = useWorkflowPanelStore((s) => s.workflowPanelState);
  const setWorkflowPanelState = useWorkflowPanelStore(
    (s) => s.setWorkflowPanelState,
  );
  const closeWorkflowPanel = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const isOpen =
    workflowPanelState.active && workflowPanelState.content === content;

  const handleClick = () => {
    if (isOpen) {
      closeWorkflowPanel();
    } else {
      setWorkflowPanelState({ active: true, content });
    }
  };

  if (iconOnly) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              disabled={isRecording}
              variant="tertiary"
              size="icon"
              className="size-10 min-w-[2.5rem]"
              onClick={handleClick}
              aria-label={label}
            >
              {leadingIcon}
            </Button>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  return (
    <Button
      disabled={isRecording}
      variant="tertiary"
      size="lg"
      onClick={handleClick}
    >
      {leadingIcon}
      <span className="mr-2">{label}</span>
      {isOpen ? (
        <ChevronUpIcon className="h-6 w-6" />
      ) : (
        <ChevronDownIcon className="h-6 w-6" />
      )}
    </Button>
  );
}

function RunButton() {
  const navigate = useNavigate();
  const { workflowPermanentId } = useParams();
  const closeWorkflowPanel = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const isRecording = useRecordingStore().isRecording;

  const handleClick = () => {
    closeWorkflowPanel();
    navigate(`/workflows/${workflowPermanentId}/run`);
  };

  return (
    <Button disabled={isRecording} size="lg" onClick={handleClick}>
      <PlayIcon className="mr-2 h-6 w-6" />
      Run
    </Button>
  );
}

function EditorActionToolbar() {
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued = Boolean(
    workflowRun && statusIsRunningOrQueued(workflowRun),
  );

  return (
    <>
      <EditorOverflowMenu />
      <BrowserModeButton />
      <SaveButton />
      {!workflowRunIsRunningOrQueued && <HistoryButton />}
      <PanelToggleButton
        content="schedules"
        label="Schedule"
        leadingIcon={<ClockIcon className="h-5 w-5" />}
        iconOnly
      />
      <PanelToggleButton content="parameters" label="Parameters" />
      <RunButton />
    </>
  );
}

function TitleSection() {
  const { title, setTitle } = useWorkflowTitleStore();
  const workflowChangesStore = useWorkflowHasChangesStore();
  const isRecording = useRecordingStore().isRecording;

  const handleChange = (newTitle: string) => {
    setTitle(newTitle);
    workflowChangesStore.setHasChanges(true);
  };

  return (
    <div className="flex h-full min-w-0 flex-1 items-center">
      <EditableNodeTitle
        editable={!isRecording}
        onChange={handleChange}
        value={title}
        titleClassName="text-xl"
        inputClassName="text-xl"
      />
    </div>
  );
}

function WorkflowHeader() {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  const collapsed = useWorkflowHeaderCollapseStore((s) => s.collapsed);
  const toggleCollapsed = useWorkflowHeaderCollapseStore((s) => s.toggle);
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const cacheKeyValueFilter = useCacheKeyValueStore((s) => s.filter);
  const isRecording = useRecordingStore().isRecording;

  const isGeneratingCode = useIsGeneratingCode({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
  });

  const { data: cacheKeyValues } = useCacheKeyValuesQuery({
    cacheKey,
    debounceMs: 100,
    filter: cacheKeyValueFilter || undefined,
    page: 1,
    workflowPermanentId,
  });

  const shouldShowCacheControls =
    !isRecording && !isGeneratingCode && (cacheKeyValues?.total_count ?? 0) > 0;

  if (!globalWorkflows) {
    return null; // this should be loaded already by some other components
  }

  const isGlobalWorkflow = globalWorkflows.some(
    (w) => w.workflow_permanent_id === workflowPermanentId,
  );

  return (
    <div
      className={cn(
        "relative flex h-full w-full rounded-xl bg-slate-elevation2 px-6 py-5",
      )}
    >
      <div
        className="flex h-full w-full justify-between"
        aria-hidden={collapsed}
        {...(collapsed ? { inert: "" } : {})}
      >
        <TitleSection />
        <div className="flex h-full shrink-0 items-center justify-end gap-4">
          {shouldShowCacheControls && <CacheKeyValueControls />}
          {isGeneratingCode && <GeneratingCodeButton />}
          {isGlobalWorkflow ? <MakeACopyButton /> : <EditorActionToolbar />}
        </div>
      </div>
      <WorkflowHeaderCollapseTab
        collapsed={collapsed}
        onToggle={toggleCollapsed}
      />
    </div>
  );
}

export { WorkflowHeader };
