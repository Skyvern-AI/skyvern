import { type KeyboardEvent, type ReactNode } from "react";
import { AxiosError } from "axios";
import {
  CalendarIcon,
  PlayIcon,
  ReloadIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";

import { getClient } from "@/api/AxiosClient";
import { SaveIcon } from "@/components/icons/SaveIcon";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useCacheKeyValueStore } from "@/store/CacheKeyValueStore";
import { useRecordingStore } from "@/store/useRecordingStore";
import { useStudioShellStore, type StudioTab } from "@/store/StudioShellStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { cn } from "@/util/utils";

import { EditableNodeTitle } from "../editor/nodes/components/EditableNodeTitle";
import { EditorOverflowMenu } from "../editor/header/EditorOverflowMenu";
import { MakeACopyButton } from "../editor/MakeACopyButton";
import { useIsGeneratingCode } from "../editor/hooks/useIsGeneratingCode";
import { useSaveWorkflow } from "../editor/hooks/useSaveWorkflow";
import { useGlobalWorkflowsQuery } from "../hooks/useGlobalWorkflowsQuery";
import { useWorkflowQuery } from "../hooks/useWorkflowQuery";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowRunsQuery } from "../hooks/useWorkflowRunsQuery";
import { studioPanelId, studioTabId } from "./constants";
import { runOutcomeFromStatus } from "./runProjections";
import { useStudioRunId } from "./useStudioRunId";

function useIsGlobalWorkflow(): boolean {
  const { workflowPermanentId } = useParams();
  const { data: globalWorkflows } = useGlobalWorkflowsQuery();
  return Boolean(
    globalWorkflows?.some(
      (w) => w.workflow_permanent_id === workflowPermanentId,
    ),
  );
}

function GeneratingCodeIndicator() {
  const { workflowPermanentId } = useParams();
  const urlRunId = useStudioRunId();
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const workflowRunId = urlRunId ?? runs?.[0]?.workflow_run_id;
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const cacheKeyValue = useCacheKeyValueStore((s) => s.cacheKeyValue);
  const isGeneratingCode = useIsGeneratingCode({
    cacheKey: workflow?.cache_key ?? "",
    cacheKeyValue,
    workflowPermanentId,
    workflowRunId,
  });

  if (!isGeneratingCode) {
    return null;
  }

  return (
    <span
      className="inline-flex items-center gap-2 rounded-md bg-slate-elevation3 px-3 py-1.5 text-sm font-medium text-muted-foreground"
      title="Generating cached code for this run"
    >
      <ReloadIcon className="size-4 animate-spin" />
      Code
    </span>
  );
}

function StudioTabs() {
  const tab = useStudioShellStore((s) => s.tab);
  const setTab = useStudioShellStore((s) => s.setTab);
  const urlRunId = useStudioRunId();
  const { workflowPermanentId } = useParams();
  const { data: urlRun } = useWorkflowRunWithWorkflowQuery(
    urlRunId ? { workflowRunId: urlRunId } : undefined,
  );
  const { data: runs } = useWorkflowRunsQuery({
    workflowPermanentId,
    page: 1,
    pageSize: 1,
  });
  const latestRun = runs?.[0];
  const hasRun = Boolean(urlRunId) || (runs?.length ?? 0) > 0;
  const outcome = runOutcomeFromStatus(
    urlRunId ? urlRun?.status : latestRun?.status,
  );

  const runBadge =
    outcome === "running"
      ? { label: "live", cls: "bg-studio-accent/20 text-studio-accent-2" }
      : outcome === "failed"
        ? { label: "failed", cls: "bg-destructive/20 text-destructive" }
        : outcome === "success"
          ? { label: "passed", cls: "bg-success/20 text-success" }
          : null;

  const tabs: Array<{ id: StudioTab; label: string; disabled: boolean }> = [
    { id: "editor", label: "Editor", disabled: false },
    { id: "browser", label: "Browser", disabled: false },
    { id: "run", label: "Run", disabled: !hasRun },
  ];

  // Roving arrow-key navigation across the enabled tabs (WAI-ARIA tabs).
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys = ["ArrowRight", "ArrowLeft", "Home", "End"];
    if (!keys.includes(event.key)) {
      return;
    }
    event.preventDefault();
    const enabled = tabs.filter((t) => !t.disabled);
    const current = enabled.findIndex((t) => t.id === tab);
    const last = enabled.length - 1;
    const nextIndex =
      event.key === "ArrowRight"
        ? (current + 1) % enabled.length
        : event.key === "ArrowLeft"
          ? (current - 1 + enabled.length) % enabled.length
          : event.key === "Home"
            ? 0
            : last;
    const next = enabled[nextIndex];
    if (next) {
      setTab(next.id);
      document.getElementById(studioTabId(next.id))?.focus();
    }
  };

  return (
    <div
      role="tablist"
      aria-label="Studio view"
      className="flex items-center gap-1"
      onKeyDown={onKeyDown}
    >
      {tabs.map((t) => {
        const selected = tab === t.id;
        return (
          <button
            key={t.id}
            id={studioTabId(t.id)}
            type="button"
            role="tab"
            aria-selected={selected}
            aria-controls={studioPanelId(t.id)}
            tabIndex={selected ? 0 : -1}
            disabled={t.disabled}
            onClick={() => !t.disabled && setTab(t.id)}
            className={cn(
              "flex items-center gap-2 rounded-md px-3 py-1.5 text-sm font-medium transition-colors",
              "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
              selected
                ? "bg-studio-accent/15 text-studio-accent-2 ring-1 ring-inset ring-studio-accent/40"
                : "text-muted-foreground hover:bg-slate-elevation3 hover:text-foreground",
              t.disabled &&
                "cursor-default opacity-50 hover:bg-transparent hover:text-muted-foreground",
            )}
          >
            {t.label}
            {t.id === "run" && runBadge ? (
              <span
                className={cn(
                  "rounded px-1.5 py-0.5 text-[10px] font-semibold",
                  runBadge.cls,
                )}
              >
                {runBadge.label}
              </span>
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

function TitleSection({ editable = true }: { editable?: boolean }) {
  const { title, setTitle } = useWorkflowTitleStore();
  const setHasChanges = useWorkflowHasChangesStore((s) => s.setHasChanges);
  const isRecording = useRecordingStore((s) => s.isRecording);
  return (
    <div className="flex min-w-0 max-w-[18rem] items-center">
      <EditableNodeTitle
        editable={editable && !isRecording}
        value={title}
        onChange={(next) => {
          setTitle(next);
          setHasChanges(true);
        }}
        titleClassName="text-base"
        inputClassName="text-base"
      />
    </div>
  );
}

function SaveButton() {
  const saving = useWorkflowHasChangesStore((s) => s.saveIsPending);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const onSave = useSaveWorkflow();
  return (
    <Button
      variant="tertiary"
      size="icon"
      className="size-9"
      disabled={isRecording}
      onClick={() => void onSave()}
      title="Save"
      aria-label="Save workflow"
    >
      {saving ? (
        <ReloadIcon className="size-5 animate-spin" />
      ) : (
        <SaveIcon className="size-5" />
      )}
    </Button>
  );
}

function PanelToggle({
  content,
  label,
  icon,
}: {
  content: "parameters" | "schedules";
  label: string;
  icon?: ReactNode;
}) {
  const state = useWorkflowPanelStore((s) => s.workflowPanelState);
  const setState = useWorkflowPanelStore((s) => s.setWorkflowPanelState);
  const close = useWorkflowPanelStore((s) => s.closeWorkflowPanel);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const isOpen = state.active && state.content === content;
  return (
    <Button
      variant="tertiary"
      size={icon ? "icon" : "lg"}
      className={icon ? "size-9" : undefined}
      disabled={isRecording}
      onClick={() => (isOpen ? close() : setState({ active: true, content }))}
      title={label}
      aria-label={label}
    >
      {icon ?? label}
    </Button>
  );
}

function RunStopButton() {
  const navigate = useNavigate();
  const { workflowPermanentId } = useParams();
  const runId = useStudioRunId();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const isRecording = useRecordingStore((s) => s.isRecording);
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  const activeRunId = workflowRun?.workflow_run_id;
  const running = runOutcomeFromStatus(workflowRun?.status) === "running";

  const cancelRun = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/workflows/runs/${activeRunId}/cancel`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflowRun", activeRunId] });
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowPermanentId, activeRunId],
      });
      queryClient.invalidateQueries({ queryKey: ["workflowRuns"] });
      toast({
        variant: "success",
        title: "Run canceled",
        description: "The agent run has been canceled.",
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to cancel run",
        description: error.message,
      });
    },
  });

  if (running && activeRunId) {
    return (
      <Dialog>
        <DialogTrigger asChild>
          <Button
            variant="destructive"
            size="lg"
            disabled={cancelRun.isPending || isRecording}
          >
            {cancelRun.isPending ? (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            ) : (
              <StopIcon className="mr-2 size-4" />
            )}
            Stop
          </Button>
        </DialogTrigger>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Stop this run?</DialogTitle>
            <DialogDescription>
              The agent will stop where it is. You can rerun the workflow at any
              time.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="secondary">Keep running</Button>
            </DialogClose>
            <DialogClose asChild>
              <Button variant="destructive" onClick={() => cancelRun.mutate()}>
                Stop run
              </Button>
            </DialogClose>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    );
  }
  return (
    <Button
      size="lg"
      className="bg-studio-accent text-studio-accent-foreground hover:bg-studio-accent/90"
      disabled={isRecording}
      onClick={() => navigate(`/workflows/${workflowPermanentId}/run`)}
    >
      <PlayIcon className="mr-2 size-4" /> Run
    </Button>
  );
}

export function StudioTopBar() {
  const isGlobalWorkflow = useIsGlobalWorkflow();
  return (
    <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-slate-elevation2 px-4">
      <TitleSection editable={!isGlobalWorkflow} />
      <StudioTabs />
      <div className="flex-1" />
      <GeneratingCodeIndicator />
      {isGlobalWorkflow ? (
        <MakeACopyButton />
      ) : (
        <div data-tour="editor-actions" className="flex items-center gap-3">
          <SaveButton />
          <PanelToggle
            content="schedules"
            label="Schedule"
            icon={<CalendarIcon className="size-5" />}
          />
          <EditorOverflowMenu />
          <div className="mx-1 h-6 w-px bg-border" />
          <PanelToggle content="parameters" label="Inputs" />
          <RunStopButton />
        </div>
      )}
    </div>
  );
}
