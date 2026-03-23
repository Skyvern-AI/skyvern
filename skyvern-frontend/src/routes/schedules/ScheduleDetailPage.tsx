import { useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  ArrowLeftIcon,
  Pencil1Icon,
  ReloadIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { WorkflowApiResponse } from "@/routes/workflows/types/workflowTypes";
import { useScheduleDetailQuery } from "./useScheduleDetailQuery";
import {
  useDeleteOrgScheduleMutation,
  useDisableScheduleMutation,
  useEnableScheduleMutation,
  useUpdateScheduleMutation,
} from "./useScheduleActions";
import {
  CRON_PRESETS,
  cronToHumanReadable,
  formatNextRun,
  getNextRuns,
  getTimezones,
  isValidCron,
} from "@/routes/workflows/editor/panels/schedulePanel/cronUtils";
import { cn } from "@/util/utils";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";

function ScheduleDetailPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { workflowPermanentId, scheduleId } = useParams();
  const credentialGetter = useCredentialGetter();
  const { data, isLoading, isError } = useScheduleDetailQuery(
    workflowPermanentId,
    scheduleId,
  );

  const titleFromState = (location.state as { workflowTitle?: string })
    ?.workflowTitle;
  const { data: workflow } = useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}`)
        .then((r) => r.data);
    },
    enabled: !!workflowPermanentId && !titleFromState,
  });
  const workflowTitle =
    titleFromState || workflow?.title || workflowPermanentId || "Schedule";

  const enableMutation = useEnableScheduleMutation();
  const disableMutation = useDisableScheduleMutation();
  const deleteMutation = useDeleteOrgScheduleMutation();
  const updateMutation = useUpdateScheduleMutation();

  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);

  // Edit mode state
  const [editing, setEditing] = useState(false);
  const [editCron, setEditCron] = useState("");
  const [editTimezone, setEditTimezone] = useState("");
  // TODO - Create shared util
  const [timezoneFilter, setTimezoneFilter] = useState<string | null>(null);

  const [editName, setEditName] = useState("");
  const [editDescription, setEditDescription] = useState("");

  const allTimezones = useMemo(() => getTimezones(), []);
  const filteredTimezones = useMemo(() => {
    if (timezoneFilter === null) return allTimezones;
    if (!timezoneFilter) return allTimezones;
    const lower = timezoneFilter.toLowerCase();
    return allTimezones.filter((tz) => tz.toLowerCase().includes(lower));
  }, [allTimezones, timezoneFilter]);
  // ! end TODO

  const editValid = isValidCron(editCron);
  const editHumanReadable = editValid ? cronToHumanReadable(editCron) : null;
  const editNextRuns = editValid ? getNextRuns(editCron, editTimezone, 5) : [];

  if (isLoading) {
    return (
      <div className="flex items-center justify-center py-20">
        <ReloadIcon className="size-6 animate-spin text-slate-400" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="py-20 text-center text-sm text-red-400">
        Failed to load schedule details.
      </div>
    );
  }

  const { schedule, next_runs } = data;
  const humanReadable = cronToHumanReadable(schedule.cron_expression);

  function startEditing() {
    setEditCron(schedule.cron_expression);
    setEditTimezone(schedule.timezone);
    setTimezoneFilter(null);
    setEditName(schedule.name ?? "");
    setEditDescription(schedule.description ?? "");
    setEditing(true);
  }

  function cancelEditing() {
    setEditing(false);
    setTimezoneFilter(null);
  }

  function handleSave() {
    if (!editValid || !workflowPermanentId || !scheduleId) return;
    updateMutation.mutate(
      {
        workflowPermanentId,
        scheduleId,
        request: {
          cron_expression: editCron,
          timezone: editTimezone,
          enabled: schedule.enabled,
          parameters: schedule.parameters,
          ...(editName && { name: editName }),
          description: editDescription || undefined,
        },
      },
      {
        onSuccess: () => setEditing(false),
      },
    );
  }

  function handleToggle(checked: boolean) {
    const item = {
      workflow_schedule_id: schedule.workflow_schedule_id,
      organization_id: schedule.organization_id,
      workflow_permanent_id: schedule.workflow_permanent_id,
      workflow_title: "",
      cron_expression: schedule.cron_expression,
      timezone: schedule.timezone,
      enabled: schedule.enabled,
      parameters: schedule.parameters,
      name: schedule.name ?? null,
      description: schedule.description ?? null,
      next_run: null,
      created_at: schedule.created_at,
      modified_at: schedule.modified_at,
    };
    if (checked) {
      enableMutation.mutate(item);
    } else {
      disableMutation.mutate(item);
    }
  }

  function handleDelete() {
    const item = {
      workflow_schedule_id: schedule.workflow_schedule_id,
      organization_id: schedule.organization_id,
      workflow_permanent_id: schedule.workflow_permanent_id,
      workflow_title: "",
      cron_expression: schedule.cron_expression,
      timezone: schedule.timezone,
      enabled: schedule.enabled,
      parameters: schedule.parameters,
      name: schedule.name ?? null,
      description: schedule.description ?? null,
      next_run: null,
      created_at: schedule.created_at,
      modified_at: schedule.modified_at,
    };
    deleteMutation.mutate(item, {
      onSuccess: () => navigate("/schedules"),
    });
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Button
          variant="ghost"
          size="icon"
          className="size-9"
          onClick={() => navigate("/schedules")}
        >
          <ArrowLeftIcon className="size-4" />
        </Button>
        <div className="flex-1">
          <h1 className="text-2xl font-normal text-slate-50">
            {schedule.name ?? workflowTitle}
          </h1>
          {schedule.description && (
            <p className="text-sm text-slate-400">{schedule.description}</p>
          )}
          <p className="text-xs text-slate-500">
            {humanReadable} · {schedule.timezone}
          </p>
          <Link
            to={`/workflows/${schedule.workflow_permanent_id}/runs`}
            className="mt-1 inline-block text-xs text-slate-400 hover:text-slate-200 hover:underline"
            onClick={(e) => e.stopPropagation()}
          >
            {workflowTitle} runs →
          </Link>
        </div>
        <Switch checked={schedule.enabled} onCheckedChange={handleToggle} />
        <Button
          variant="destructive"
          size="icon"
          className="size-9"
          onClick={() => setDeleteDialogOpen(true)}
        >
          <TrashIcon className="size-4" />
        </Button>
      </div>

      {/* Content grid */}
      <div className="grid grid-cols-2 gap-6">
        {/* Schedule Configuration */}
        <div className="rounded-lg border border-slate-700 p-4">
          <div className="mb-4 flex items-center justify-between">
            <h3 className="text-sm text-slate-400">Schedule Configuration</h3>
            {!editing && (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 gap-1.5 px-2 text-xs"
                onClick={startEditing}
              >
                <Pencil1Icon className="size-3" />
                Edit
              </Button>
            )}
          </div>

          {editing ? (
            <div className="space-y-4">
              {/* Name */}
              <div className="space-y-1.5">
                <Label className="text-xs">Name</Label>
                <Input
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  placeholder="Schedule name"
                  className="h-8 text-sm"
                />
              </div>

              {/* Description */}
              <div className="space-y-1.5">
                <Label className="text-xs">Description</Label>
                <Input
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  placeholder="Add a description..."
                  className="h-8 text-sm"
                />
              </div>

              {/* Cron Presets */}
              <div className="space-y-2">
                <Label className="text-xs">Quick Presets</Label>
                <div className="flex flex-wrap gap-1.5">
                  {CRON_PRESETS.map((preset) => (
                    <Button
                      key={preset.label}
                      variant={
                        editCron === preset.expression ? "default" : "secondary"
                      }
                      size="sm"
                      className="h-6 text-xs"
                      onClick={() => setEditCron(preset.expression)}
                    >
                      {preset.label}
                    </Button>
                  ))}
                </div>
              </div>

              {/* Cron Expression */}
              <div className="space-y-1.5">
                <Label className="text-xs">Cron Expression</Label>
                <Input
                  value={editCron}
                  onChange={(e) => setEditCron(e.target.value)}
                  placeholder="* * * * *"
                  className={cn(
                    "h-8 text-sm",
                    !editValid && editCron && "border-destructive",
                  )}
                />
                {editHumanReadable && (
                  <p className="text-xs text-slate-400">{editHumanReadable}</p>
                )}
                {!editValid && editCron && (
                  <p className="text-xs text-destructive">
                    Invalid cron expression
                  </p>
                )}
              </div>

              {/* Timezone */}
              <div className="space-y-1.5">
                <Label className="text-xs">Timezone</Label>
                <Input
                  value={timezoneFilter ?? editTimezone}
                  onChange={(e) => setTimezoneFilter(e.target.value)}
                  onFocus={(e) => e.currentTarget.select()}
                  onBlur={() => {
                    if (
                      filteredTimezones.length === 1 &&
                      filteredTimezones[0] !== undefined
                    ) {
                      setEditTimezone(filteredTimezones[0]);
                    }
                    setTimezoneFilter(null);
                  }}
                  placeholder="Search timezones..."
                  className="h-8 text-sm"
                />
                {timezoneFilter !== null && (
                  <div className="max-h-32 overflow-y-auto rounded-md border border-slate-700 bg-slate-elevation3">
                    {filteredTimezones.slice(0, 15).map((tz) => (
                      <button
                        key={tz}
                        type="button"
                        className={cn(
                          "w-full px-3 py-1 text-left text-xs hover:bg-slate-700",
                          tz === editTimezone && "bg-slate-700 text-slate-50",
                        )}
                        onMouseDown={(e) => {
                          e.preventDefault();
                          setEditTimezone(tz);
                          setTimezoneFilter(null);
                        }}
                      >
                        {tz}
                      </button>
                    ))}
                    {filteredTimezones.length === 0 && (
                      <div className="px-3 py-1.5 text-xs text-slate-500">
                        No timezones found
                      </div>
                    )}
                  </div>
                )}
                <p className="text-xs text-slate-500">
                  Current: {editTimezone}
                </p>
              </div>

              {/* Preview next runs */}
              {editNextRuns.length > 0 && (
                <div className="space-y-1">
                  <Label className="text-xs">Next Runs Preview</Label>
                  <div className="space-y-0.5">
                    {editNextRuns.map((run) => (
                      <p
                        key={run.toISOString()}
                        className="text-xs text-slate-500"
                      >
                        {formatNextRun(run, editTimezone)}
                      </p>
                    ))}
                  </div>
                </div>
              )}

              {/* Save / Cancel */}
              <div className="flex gap-2 pt-1">
                <Button
                  size="sm"
                  className="h-7 text-xs"
                  disabled={!editValid || updateMutation.isPending}
                  onClick={handleSave}
                >
                  {updateMutation.isPending ? "Saving..." : "Save"}
                </Button>
                <Button
                  variant="secondary"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={cancelEditing}
                >
                  Cancel
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-3">
              <div className="flex items-start justify-between">
                <span className="text-sm text-slate-400">Frequency</span>
                <span className="text-sm text-slate-50">{humanReadable}</span>
              </div>
              <div className="flex items-start justify-between">
                <span className="text-sm text-slate-400">Timezone</span>
                <span className="text-sm text-slate-50">
                  {schedule.timezone}
                </span>
              </div>
              <div className="flex items-start justify-between">
                <span className="text-sm text-slate-400">Cron</span>
                <code className="font-mono text-xs text-slate-50">
                  {schedule.cron_expression}
                </code>
              </div>
            </div>
          )}
        </div>

        {/* Details + Upcoming Runs */}
        <div className="space-y-6">
          <div className="rounded-lg border border-slate-700 p-4">
            <h3 className="mb-4 text-sm text-slate-400">Details</h3>
            <div className="space-y-2">
              <div className="flex items-start justify-between">
                <span className="text-sm text-slate-400">Created</span>
                <span
                  className="text-sm text-slate-50"
                  title={basicTimeFormat(schedule.created_at)}
                >
                  {basicLocalTimeFormat(schedule.created_at)}
                </span>
              </div>
              <div className="flex items-start justify-between">
                <span className="text-sm text-slate-400">Last Modified</span>
                <span
                  className="text-sm text-slate-50"
                  title={basicTimeFormat(schedule.modified_at)}
                >
                  {basicLocalTimeFormat(schedule.modified_at)}
                </span>
              </div>
            </div>
          </div>

          <div className="rounded-lg border border-slate-700 p-4">
            <h3 className="mb-4 text-sm text-slate-400">Upcoming Runs</h3>
            <p className="mb-2 text-xs text-slate-400">
              Next {next_runs.length} runs
            </p>
            <div className="space-y-0.5">
              {next_runs.map((run) => (
                <p key={run} className="text-xs text-slate-500">
                  {formatNextRun(new Date(run), schedule.timezone)}
                </p>
              ))}
            </div>
          </div>
        </div>
      </div>
      <Dialog
        open={deleteDialogOpen}
        onOpenChange={(open) => {
          if (!open && !deleteMutation.isPending) {
            setDeleteDialogOpen(false);
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
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteDialogOpen(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={handleDelete}
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

export { ScheduleDetailPage };
