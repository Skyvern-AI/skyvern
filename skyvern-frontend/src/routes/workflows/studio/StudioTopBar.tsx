import { type ReactNode } from "react";
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
import { useWorkflowTitleStore } from "@/store/WorkflowTitleStore";
import { cn } from "@/util/utils";

import { EditableNodeTitle } from "../editor/nodes/components/EditableNodeTitle";
import { EditorOverflowMenu } from "../editor/header/EditorOverflowMenu";
import { MakeACopyButton } from "../editor/MakeACopyButton";
import { useSaveWorkflow } from "../editor/hooks/useSaveWorkflow";
import { useIsGlobalWorkflow } from "../hooks/useIsGlobalWorkflow";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { STUDIO_PANES_PARAM } from "./panes";
import { runOutcomeFromStatus } from "./runProjections";
import { useStudioPanes } from "./useStudioPanes";
import { useStudioRunId } from "./useStudioRunId";

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
      size={icon ? "icon" : "default"}
      disabled={isRecording}
      aria-pressed={isOpen}
      className={cn(
        isOpen &&
          "border-studio-accent/40 bg-studio-accent/15 text-foreground hover:bg-studio-accent/20",
      )}
      onClick={() => (isOpen ? close() : setState({ active: true, content }))}
      title={label}
      aria-label={label}
    >
      {icon ?? label}
    </Button>
  );
}

export function RunStopButton() {
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
  const { resolveLivePanes } = useStudioPanes();
  // ?bl= marks the URL run as a block run; a full run can start alongside it
  // (they execute concurrently), so Run stays available next to Stop.
  const isBlockRun = searchParams.has("bl");

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
  const startFullRun = () =>
    navigate(
      `/agents/${workflowPermanentId}/run?${STUDIO_PANES_PARAM}=${resolveLivePanes().join(",")}`,
    );

  if (running && activeRunId) {
    const stopDialog = (
      <Dialog>
        <DialogTrigger asChild>
          <Button
            variant="destructive"
            size="default"
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
    if (!isBlockRun) {
      return stopDialog;
    }
    return (
      <>
        {stopDialog}
        <Dialog>
          <DialogTrigger asChild>
            <Button size="default" disabled={isRecording}>
              <PlayIcon className="mr-2 size-4" /> Run
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Start a full run?</DialogTitle>
              <DialogDescription>
                A block run is still executing. It will keep running — you can
                watch it in the Browser pane while the Timeline pane switches to
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
  return (
    <Button size="default" disabled={isRecording} onClick={startFullRun}>
      <PlayIcon className="mr-2 size-4" /> Run
    </Button>
  );
}

export function StudioTopBar() {
  const isGlobalWorkflow = useIsGlobalWorkflow();
  return (
    <div className="flex h-14 shrink-0 items-center gap-3 border-b border-border bg-slate-elevation2 px-4">
      <TitleSection editable={!isGlobalWorkflow} />
      <div className="flex-1" />
      {isGlobalWorkflow ? (
        <MakeACopyButton />
      ) : (
        <div data-tour="editor-actions" className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            <SaveButton />
            <PanelToggle
              content="schedules"
              label="Schedule"
              icon={<CalendarIcon className="size-5" />}
            />
            <EditorOverflowMenu triggerClassName="size-9" />
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
