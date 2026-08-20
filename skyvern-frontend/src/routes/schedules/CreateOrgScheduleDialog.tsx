import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
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
import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type {
  WorkflowApiResponse,
  Parameter,
} from "@/routes/workflows/types/workflowTypes";
import {
  getLocalTimezone,
  isValidCron,
  meetsMinCronInterval,
} from "@/routes/workflows/editor/panels/schedulePanel/cronUtils";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { ScheduleConfigFields } from "@/routes/workflows/components/ScheduleConfigFields";
import { ScheduleParametersSection } from "@/routes/workflows/components/ScheduleParametersSection";
import { buildScheduleParametersPayload } from "@/routes/workflows/components/scheduleParameters";
import { useScheduleParameterState } from "@/routes/workflows/hooks/useScheduleParameterState";
import { useCreateOrgScheduleMutation } from "./useCreateOrgScheduleMutation";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
};

function CreateOrgScheduleDialog({ open, onOpenChange }: Readonly<Props>) {
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();
  const createMutation = useCreateOrgScheduleMutation();

  const [workflowSearch, setWorkflowSearch] = useState("");
  const [workflowPickerOpen, setWorkflowPickerOpen] = useState(false);
  const [selectedWorkflow, setSelectedWorkflow] =
    useState<WorkflowApiResponse | null>(null);
  const [cronExpression, setCronExpression] = useState("0 9 * * *");
  const [timezone, setTimezone] = useState(getLocalTimezone);

  const [scheduleName, setScheduleName] = useState("");
  const [scheduleDescription, setScheduleDescription] = useState("");

  const { data: workflows = [] } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", "scheduleDialogPicker", workflowSearch],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", "1");
      params.append("page_size", "20");
      params.append("only_workflows", "true");
      if (workflowSearch) {
        params.append("search_key", workflowSearch);
      }
      return client.get("/workflows", { params }).then((r) => r.data);
    },
    enabled: open,
  });

  const { data: selectedWorkflowDetail } = useWorkflowQuery({
    workflowPermanentId: selectedWorkflow?.workflow_permanent_id,
  });

  // The workflow detail (and therefore the parameter definitions) is loaded
  // iff the resolved workflow id matches the selection. While the request is
  // still in flight or stale, the form must not allow submission — otherwise
  // we'd POST without `parameters` for a workflow that has required inputs
  // and the backend would 400 with "Missing schedule parameters".
  const workflowDetailLoaded =
    selectedWorkflow !== null &&
    selectedWorkflowDetail?.workflow_permanent_id ===
      selectedWorkflow.workflow_permanent_id;

  const workflowParameters = useMemo<ReadonlyArray<Parameter>>(() => {
    if (
      !selectedWorkflowDetail ||
      selectedWorkflowDetail.workflow_permanent_id !==
        selectedWorkflow?.workflow_permanent_id
    ) {
      return [];
    }

    return selectedWorkflowDetail.workflow_definition.parameters;
  }, [selectedWorkflow, selectedWorkflowDetail]);
  const {
    values: parameters,
    errors: parameterErrors,
    handleChange: handleParameterChange,
    validate: validateParameters,
    reset: resetParameters,
    clear: clearParameters,
  } = useScheduleParameterState(workflowParameters);

  // Re-seed parameter state when (a) the user switches workflows or
  // (b) the parameter definitions for the currently-selected workflow
  // arrive from a still-in-flight react-query fetch. We track the
  // (workflowId, paramsLength) tuple in a ref so refetches that produce
  // an identical parameter set don't clobber user-typed values.
  const selectedWorkflowId = selectedWorkflow?.workflow_permanent_id ?? null;
  const lastSeededRef = useRef<{ id: string | null; count: number }>({
    id: null,
    count: 0,
  });
  useEffect(() => {
    const last = lastSeededRef.current;
    const next = { id: selectedWorkflowId, count: workflowParameters.length };
    if (last.id !== next.id || last.count !== next.count) {
      lastSeededRef.current = next;
      resetParameters();
    }
  }, [selectedWorkflowId, workflowParameters, resetParameters]);

  const valid = isValidCron(cronExpression);
  const cronAccepted = valid && meetsMinCronInterval(cronExpression);

  function resetForm() {
    setWorkflowSearch("");
    setWorkflowPickerOpen(false);
    setSelectedWorkflow(null);
    setCronExpression("0 9 * * *");
    setTimezone(getLocalTimezone());
    setScheduleName("");
    setScheduleDescription("");
    clearParameters();
  }

  function handleSubmit() {
    if (!selectedWorkflow || !workflowDetailLoaded) return;
    const parametersValid = validateParameters();
    if (!cronAccepted || !parametersValid) return;
    const payload = buildScheduleParametersPayload(
      parameters,
      workflowParameters,
    );
    createMutation.mutate(
      {
        workflowPermanentId: selectedWorkflow.workflow_permanent_id,
        request: {
          cron_expression: cronExpression,
          timezone,
          ...(scheduleName && { name: scheduleName }),
          ...(scheduleDescription && { description: scheduleDescription }),
          ...(payload && { parameters: payload }),
        },
      },
      {
        onSuccess: (data) => {
          onOpenChange(false);
          resetForm();
          navigate(
            `/schedules/${selectedWorkflow.workflow_permanent_id}/${data.schedule.workflow_schedule_id}`,
          );
        },
      },
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(nextOpen) => {
        if (!nextOpen) resetForm();
        onOpenChange(nextOpen);
      }}
    >
      <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Create Schedule</DialogTitle>
          <DialogDescription>
            Choose an agent and configure when it should run automatically.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-6 py-4">
          {/* Agent Picker */}
          <div className="space-y-2">
            <Label>Agent</Label>
            {selectedWorkflow ? (
              <div className="flex items-center justify-between rounded-md border border-slate-700 bg-slate-elevation3 px-3 py-2">
                <span className="text-sm">{selectedWorkflow.title}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-6 px-2 text-xs"
                  onClick={() => setSelectedWorkflow(null)}
                >
                  Change
                </Button>
              </div>
            ) : (
              <>
                <Input
                  value={workflowSearch}
                  onChange={(e) => setWorkflowSearch(e.target.value)}
                  onFocus={() => setWorkflowPickerOpen(true)}
                  placeholder="Search agents..."
                />
                {workflowPickerOpen && (
                  <div className="max-h-40 overflow-y-auto rounded-md border border-slate-700 bg-slate-elevation3">
                    {workflows.map((wf) => (
                      <button
                        key={wf.workflow_permanent_id}
                        type="button"
                        className="w-full px-3 py-2 text-left text-sm hover:bg-slate-700"
                        onClick={() => {
                          setSelectedWorkflow(wf);
                          setWorkflowSearch("");
                        }}
                      >
                        {wf.title}
                      </button>
                    ))}
                    {workflows.length === 0 && (
                      <div className="px-3 py-2 text-sm text-slate-500">
                        No agents found
                      </div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>

          {/* Schedule Name & Description */}
          <div className="space-y-2">
            <Label>Name (optional)</Label>
            <Input
              placeholder="Auto-generated if empty"
              value={scheduleName}
              onChange={(e) => setScheduleName(e.target.value)}
            />
          </div>
          <div className="space-y-2">
            <Label>Description (optional)</Label>
            <Input
              placeholder="Add a description..."
              value={scheduleDescription}
              onChange={(e) => setScheduleDescription(e.target.value)}
            />
          </div>

          <ScheduleParametersSection
            parameters={workflowParameters}
            values={parameters}
            onChange={handleParameterChange}
            errors={parameterErrors}
            disabled={createMutation.isPending}
          />

          <ScheduleConfigFields
            cronExpression={cronExpression}
            timezone={timezone}
            onCronChange={setCronExpression}
            onTimezoneChange={setTimezone}
            disabled={createMutation.isPending}
          />
        </div>

        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={
              !cronAccepted || !workflowDetailLoaded || createMutation.isPending
            }
            onClick={handleSubmit}
          >
            {createMutation.isPending ? "Creating..." : "Create Schedule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { CreateOrgScheduleDialog };
