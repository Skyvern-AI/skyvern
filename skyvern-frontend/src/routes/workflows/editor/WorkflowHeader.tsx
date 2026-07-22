import {
  CalendarIcon,
  ChevronDownIcon,
  ChevronUpIcon,
  PlayIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { useWorkflowPermanentId } from "@/routes/workflows/WorkflowPermanentIdContext";

import { BrowserIcon } from "@/components/icons/BrowserIcon";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useIsGlobalWorkflow } from "../hooks/useIsGlobalWorkflow";
import { MakeACopyButton } from "./MakeACopyButton";
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
import { useWorkflowHeaderCollapseStore } from "./useWorkflowHeaderCollapseStore";
import { WorkflowHeaderCollapseTab } from "./WorkflowHeaderCollapseTab";

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

function BrowserModeButton() {
  const navigate = useNavigate();
  const workflowPermanentId = useWorkflowPermanentId();
  const debugStore = useDebugStore();
  const recordingStore = useRecordingStore();
  const { data: workflowRun } = useWorkflowRunQuery();
  const workflowRunIsRunningOrQueued = Boolean(
    workflowRun && statusIsRunningOrQueued(workflowRun),
  );

  const handleClick = () => {
    const target = debugStore.isDebugMode ? "edit" : "build";
    navigate(`/agents/${workflowPermanentId}/${target}`);
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
  const workflowPermanentId = useWorkflowPermanentId();
  const closeWorkflowPanel = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const isRecording = useRecordingStore().isRecording;

  const handleClick = () => {
    closeWorkflowPanel();
    navigate(`/agents/${workflowPermanentId}/run`);
  };

  return (
    <Button disabled={isRecording} size="lg" onClick={handleClick}>
      <PlayIcon className="mr-2 h-6 w-6" />
      Run
    </Button>
  );
}

function EditorActionToolbar() {
  return (
    <div data-tour="editor-actions" className="flex items-center gap-2">
      <BrowserModeButton />
      <SaveButton />
      <PanelToggleButton
        content="schedules"
        label="Schedule"
        leadingIcon={<CalendarIcon className="h-5 w-5" />}
        iconOnly
      />
      <EditorOverflowMenu />
      <div
        className="mx-1 h-6 w-px bg-muted dark:bg-slate-700"
        aria-hidden="true"
      />
      <PanelToggleButton content="parameters" label="Inputs" />
      <RunButton />
    </div>
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
  const workflowPermanentId = useWorkflowPermanentId();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKey = workflow?.cache_key ?? "";

  const collapsed = useWorkflowHeaderCollapseStore((s) => s.collapsed);
  const toggleCollapsed = useWorkflowHeaderCollapseStore((s) => s.toggle);
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);

  const isGeneratingCode = useIsGeneratingCode({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
  });

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
