import { type ReactNode, useMemo, useState } from "react";
import { AxiosError } from "axios";
import {
  CalendarIcon,
  PlayIcon,
  ReloadIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";

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
import { useRecordingStore } from "@/store/useRecordingStore";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowPanelStore } from "@/store/WorkflowPanelStore";
import { useWorkflowSnapshotStore } from "@/store/WorkflowSnapshotStore";
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { statusIsFinalized } from "@/routes/tasks/types";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";

import { EditableNodeTitle } from "../editor/nodes/components/EditableNodeTitle";
import { EditorOverflowMenu } from "../editor/header/EditorOverflowMenu";
import { MakeACopyButton } from "../editor/MakeACopyButton";
import { WorkflowChangesList } from "../editor/WorkflowChangesList";
import {
  isDraftDirty,
  summarizeWorkflowChanges,
} from "../editor/workflowChangesSummary";
import { useSaveWorkflow } from "../editor/hooks/useSaveWorkflow";
import { useToggleHistoryPanel } from "../editor/hooks/useToggleHistoryPanel";
import { useIsGlobalWorkflow } from "../hooks/useIsGlobalWorkflow";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getRerunNavigationState } from "../utils";
import { runOutcomeFromStatus } from "./runProjections";
import { ControlTooltip } from "./ControlTooltip";
import { PaneHeaderDivider } from "./PaneHeaderDivider";
import { StudioPaneToggles } from "./StudioPaneToggles";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunId } from "./useStudioRunId";
import { useStudioWorkflowDeletedAt } from "./StudioShellContext";

function TitleSection({ editable = true }: { editable?: boolean }) {
  const { title, setTitle } = useWorkflowTitleStore();
  const setHasChanges = useWorkflowHasChangesStore((s) => s.setHasChanges);
  const isRecording = useRecordingStore((s) => s.isRecording);
  return (
    <div className="flex min-w-0 max-w-[19rem] items-center">
      <EditableNodeTitle
        editable={editable && !isRecording}
        value={title}
        onChange={(next) => {
          setTitle(next);
          setHasChanges(true);
        }}
        titleClassName="px-2 text-base"
        inputClassName="px-2 text-base"
      />
    </div>
  );
}

export function SaveButton() {
  const saving = useWorkflowHasChangesStore((s) => s.saveIsPending);
  const getSaveData = useWorkflowHasChangesStore((s) => s.getSaveData);
  // contentDirty reflects real user edits vs the clean baseline snapshot, so
  // post-load canvas materialization (login autofill) doesn't light the dot.
  // It's debounced, so the click handler recomputes dirtiness synchronously
  // instead of gating on it (see onClick).
  const contentDirty = useWorkflowSnapshotStore((s) => s.contentDirty);
  const snapshot = useWorkflowSnapshotStore((s) => s.snapshot);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const onSave = useSaveWorkflow();
  const [confirmOpen, setConfirmOpen] = useState(false);

  // Compute once when the confirm dialog opens; the canvas is behind the modal
  // and can't be edited while it's up, so the summary stays valid.
  const changes = useMemo(() => {
    if (!confirmOpen) {
      return [];
    }
    try {
      const saveData = getSaveData();
      return saveData ? summarizeWorkflowChanges(saveData, snapshot) : [];
    } catch (error) {
      console.error("Failed to summarize workflow changes", error);
      return [];
    }
  }, [confirmOpen, getSaveData, snapshot]);

  return (
    <>
      <ControlTooltip content="Save workflow" blocked={isRecording}>
        <Button
          variant="outline"
          size="icon"
          className="relative h-8 w-8 border-border bg-transparent shadow-none"
          disabled={isRecording}
          onClick={() => {
            // Recompute dirtiness synchronously from the same source as the
            // summary (incl. the YAML draft). contentDirty is debounced and
            // canvas-only, so gating the confirmation on it would skip it for a
            // YAML edit or an edit-then-save inside the debounce window.
            let dirty = false;
            try {
              const saveData = getSaveData();
              dirty = saveData ? isDraftDirty(saveData, snapshot) : false;
            } catch (error) {
              console.error("Failed to check workflow changes", error);
            }
            // onSave rejects on a failed save (already toasted by its onError);
            // swallow so it isn't an unhandled rejection.
            if (dirty) {
              setConfirmOpen(true);
            } else {
              void onSave().catch(() => {});
            }
          }}
          aria-label={
            contentDirty ? "Save workflow (unsaved changes)" : "Save workflow"
          }
        >
          {saving ? (
            <ReloadIcon className="size-4 animate-spin" />
          ) : (
            <SaveIcon className="size-4" />
          )}
          {contentDirty && !saving && (
            <span
              aria-hidden
              className="absolute -right-0.5 -top-0.5 size-1.5 rounded-full bg-primary"
            />
          )}
        </Button>
      </ControlTooltip>
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Saving Changes</DialogTitle>
            <DialogDescription>
              The changes below are going to be saved:
            </DialogDescription>
          </DialogHeader>
          <WorkflowChangesList changes={changes} />
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="secondary">Cancel</Button>
            </DialogClose>
            <Button
              disabled={saving}
              onClick={() => {
                // Close only on success; a failed save (already toasted by
                // onSave's onError) keeps the list open for retry, so swallow
                // the rejection rather than leaving it unhandled.
                void onSave()
                  .then(() => {
                    if (!useWorkflowHasChangesStore.getState().hasChanges) {
                      setConfirmOpen(false);
                    }
                  })
                  .catch(() => {});
              }}
            >
              {saving && <ReloadIcon className="mr-2 size-4 animate-spin" />}
              Save changes
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
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
  const button = (
    <Button
      variant="outline"
      size={icon ? "icon" : "default"}
      disabled={isRecording}
      aria-pressed={isOpen}
      className={cn(
        "border-border bg-transparent shadow-none",
        icon ? "h-8 w-8" : "h-8 px-3 text-xs",
        isOpen && "bg-accent text-accent-foreground hover:bg-accent/80",
      )}
      onClick={() => (isOpen ? close() : setState({ active: true, content }))}
      aria-label={label}
    >
      {icon ?? label}
    </Button>
  );
  // Only icon-only toggles tooltip; a text label is self-describing.
  if (!icon) {
    return button;
  }
  return (
    <ControlTooltip content={label} blocked={isRecording}>
      {button}
    </ControlTooltip>
  );
}

// stopOnly: global (read-only) workflows can't start runs from the studio, but
// runs started elsewhere (e.g. the recipe pages run templates in place) still
// land here and must be stoppable — render Stop when active, nothing otherwise.
export function RunStopButton({ stopOnly = false }: { stopOnly?: boolean }) {
  const navigate = useNavigate();
  const { workflowPermanentId } = useParams();
  const runId = useStudioRunId();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const isRecording = useRecordingStore((s) => s.isRecording);
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery(
    runId ? { workflowRunId: runId } : undefined,
  );
  const activeRunId = workflowRun?.workflow_run_id;
  const running = runOutcomeFromStatus(workflowRun?.status) === "running";
  // ?bl= marks the URL run as a block run; a full run can start alongside it
  // (they execute concurrently), so Run stays available next to Stop.
  const isBlockRun = searchParams.has("bl");
  const rerunEligible = Boolean(
    workflowRun &&
    // keepPreviousData can surface a prior run after the focused run clears/changes;
    // only treat it as the focused run when its id matches the URL.
    workflowRun.workflow_run_id === runId &&
    statusIsFinalized(workflowRun) &&
    workflowRun.task_v2 === null &&
    !isBlockRun &&
    !workflowRun.workflow?.deleted_at,
  );

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

  // ?panes= rides through the run form so the post-start navigate restores
  // this exact layout (plus the run surfaces appended) instead of remapping.
  const startFullRun = () => {
    const path = `/agents/${workflowPermanentId}/run`;
    if (rerunEligible && workflowRun) {
      navigate(path, { state: getRerunNavigationState(workflowRun) });
      return;
    }
    navigate(path);
  };

  if (running && activeRunId) {
    const stopDialog = (
      <Dialog>
        <DialogTrigger asChild>
          <Button
            variant="destructive"
            size="default"
            className="h-8 px-3"
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
    if (stopOnly || !isBlockRun) {
      return stopDialog;
    }
    return (
      <>
        {stopDialog}
        <Dialog>
          <DialogTrigger asChild>
            <Button
              size="default"
              className="h-8 border border-transparent px-3"
              disabled={isRecording}
            >
              <PlayIcon className="mr-2 size-4" /> Run
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Start a full run?</DialogTitle>
              <DialogDescription>
                A block run is still executing. It will keep running — you can
                watch it in the Browser pane while the Overview pane switches to
                the new full run.
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="secondary">Not now</Button>
              </DialogClose>
              <DialogClose asChild>
                <Button onClick={startFullRun}>Start full run</Button>
              </DialogClose>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </>
    );
  }
  if (stopOnly) {
    return null;
  }
  return (
    <Button
      size="default"
      className="h-8 border border-transparent px-3"
      disabled={isRecording}
      onClick={startFullRun}
    >
      <PlayIcon className="mr-2 size-4" />
      {rerunEligible ? "Re-run" : "Run"}
    </Button>
  );
}

export function StudioTopBar() {
  const isGlobalWorkflow = useIsGlobalWorkflow();
  const workflowDeletedAt = useStudioWorkflowDeletedAt();
  const { setOpenPanes } = useStudioPanes();
  const toggleHistoryPanel = useToggleHistoryPanel();
  // Version comparison renders in the editor canvas: collapse to an
  // editor-only layout on entry (an explicit override, like the full-run
  // reset). Exiting doesn't restore the previous set — reopen as needed.
  const openVersionHistory = () => {
    setOpenPanes(["editor"]);
    toggleHistoryPanel();
  };
  return (
    <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-slate-elevation2 px-4">
      <TitleSection editable={!isGlobalWorkflow && !workflowDeletedAt} />
      <PaneHeaderDivider />
      <StudioPaneToggles />
      <div className="min-w-3 flex-1" />
      {workflowDeletedAt ? (
        // Legacy run-header tag idiom; every workflow-mutating action (save,
        // schedule, inputs, run) is gone with the agent.
        <span
          title={basicTimeFormat(workflowDeletedAt)}
          className="shrink-0 text-xs text-muted-foreground"
        >
          Agent deleted on {basicLocalTimeFormat(workflowDeletedAt)}
        </span>
      ) : isGlobalWorkflow ? (
        <div className="flex items-center gap-2">
          <RunStopButton stopOnly />
          <MakeACopyButton />
        </div>
      ) : (
        <div data-tour="editor-actions" className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            <SaveButton />
            <PanelToggle
              content="schedules"
              label="Schedule"
              icon={<CalendarIcon className="size-4" />}
            />
            <EditorOverflowMenu
              triggerClassName="h-8 w-8 rounded-md border border-border bg-transparent shadow-none"
              onVersionHistory={openVersionHistory}
              embedded
            />
          </div>
          <div className="h-6 w-px bg-border" aria-hidden />
          <div className="flex items-center gap-2">
            <PanelToggle content="parameters" label="Inputs" />
            <RunStopButton />
          </div>
        </div>
      )}
    </div>
  );
}
