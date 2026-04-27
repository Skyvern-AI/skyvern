import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useWorkflowSchedulesQuery } from "@/routes/workflows/hooks/useWorkflowSchedulesQuery";
import {
  useCreateScheduleMutation,
  useToggleScheduleMutation,
  useDeleteScheduleMutation,
} from "@/routes/workflows/hooks/useScheduleMutations";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { ScheduleCard } from "./ScheduleCard";
import { CreateScheduleDialog } from "./CreateScheduleDialog";
import { ReloadIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { useParams } from "react-router-dom";

function WorkflowSchedulePanel() {
  const {
    data: schedules,
    isLoading,
    isError,
    error,
  } = useWorkflowSchedulesQuery();
  const createSchedule = useCreateScheduleMutation();
  const toggleSchedule = useToggleScheduleMutation();
  const deleteSchedule = useDeleteScheduleMutation();
  const [deleteDialogState, setDeleteDialogState] = useState<{
    open: boolean;
    scheduleId: string | null;
  }>({ open: false, scheduleId: null });

  const { workflowPermanentId } = useParams();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });
  const workflowParameters = workflow?.workflow_definition.parameters ?? [];

  const handleCreate = (
    cronExpression: string,
    timezone: string,
    name: string,
    description: string,
    parameters: Record<string, unknown> | null,
    callbacks: { onSuccess: () => void },
  ) => {
    createSchedule.mutate(
      {
        cron_expression: cronExpression,
        timezone,
        enabled: true,
        ...(name && { name }),
        ...(description && { description }),
        ...(parameters && { parameters }),
      },
      { onSuccess: callbacks.onSuccess },
    );
  };

  const handleToggle = (scheduleId: string, enabled: boolean) => {
    toggleSchedule.mutate({ scheduleId, enabled });
  };

  const handleDeleteConfirm = () => {
    if (deleteDialogState.scheduleId) {
      deleteSchedule.mutate(deleteDialogState.scheduleId, {
        onSettled: () => {
          setDeleteDialogState({ open: false, scheduleId: null });
        },
      });
    }
  };

  return (
    <div className="flex h-full w-[22rem] flex-col rounded-lg border border-slate-700 bg-slate-elevation3">
      <div className="flex items-center justify-between border-b border-slate-700 px-4 py-4">
        <h3 className="text-sm font-normal text-slate-50">
          Schedules
          {schedules && schedules.length > 0 ? ` (${schedules.length})` : ""}
        </h3>
        <CreateScheduleDialog
          workflowParameters={workflowParameters}
          onSubmit={handleCreate}
          isPending={createSchedule.isPending}
        />
      </div>

      <ScrollArea>
        <ScrollAreaViewport className="max-h-[calc(100vh-16rem)]">
          <div className="flex flex-col gap-3 px-4 py-2">
            {isLoading && (
              <div className="flex items-center justify-center py-8">
                <ReloadIcon className="size-5 animate-spin text-slate-400" />
              </div>
            )}
            {isError && (
              <div className="py-8 text-center text-sm text-red-400">
                Failed to load schedules.
                {error?.message && (
                  <span className="block text-xs text-slate-500">
                    {error.message}
                  </span>
                )}
              </div>
            )}
            {!isLoading &&
              !isError &&
              (!schedules || schedules.length === 0) && (
                <div className="py-8 text-center text-sm text-slate-500">
                  No schedules configured.
                  <br />
                  Click &quot;Add&quot; to create one.
                </div>
              )}
            {schedules?.map((schedule) => (
              <ScheduleCard
                key={schedule.workflow_schedule_id}
                schedule={schedule}
                isToggling={toggleSchedule.isPending}
                onToggle={handleToggle}
                onDelete={(id) =>
                  setDeleteDialogState({ open: true, scheduleId: id })
                }
              />
            ))}
          </div>
        </ScrollAreaViewport>
      </ScrollArea>

      <Dialog
        open={deleteDialogState.open}
        onOpenChange={(open) => {
          if (!open && !deleteSchedule.isPending) {
            setDeleteDialogState({ open: false, scheduleId: null });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Schedule</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this schedule? This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={deleteSchedule.isPending}
              onClick={() =>
                setDeleteDialogState({ open: false, scheduleId: null })
              }
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteSchedule.isPending}
              onClick={handleDeleteConfirm}
            >
              {deleteSchedule.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { WorkflowSchedulePanel };
