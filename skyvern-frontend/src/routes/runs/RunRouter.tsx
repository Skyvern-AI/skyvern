/**
 * A router component that handles both workflow runs (wr_xxx) and task runs (tsk_xxx)
 * under the /runs/:runId path, discriminating based on ID prefix.
 */

import { Navigate, Route, Routes, useParams } from "react-router-dom";
import { useMemo } from "react";

import { PageLayout } from "@/components/PageLayout";
import { Status404 } from "@/components/Status404";
import { StepArtifactsLayout } from "@/routes/tasks/detail/StepArtifactsLayout";
import { TaskActions } from "@/routes/tasks/detail/TaskActions";
import { TaskDetails } from "@/routes/tasks/detail/TaskDetails";
import { TaskParameters } from "@/routes/tasks/detail/TaskParameters";
import { TaskRecording } from "@/routes/tasks/detail/TaskRecording";
import { WorkflowRun } from "@/routes/workflows/WorkflowRun";
import { WorkflowPostRunParameters } from "@/routes/workflows/workflowRun/WorkflowPostRunParameters";
import { WorkflowRunOutput } from "@/routes/workflows/workflowRun/WorkflowRunOutput";
import { WorkflowRunOverview } from "@/routes/workflows/workflowRun/WorkflowRunOverview";
import { WorkflowRunRecording } from "@/routes/workflows/workflowRun/WorkflowRunRecording";
import { WorkflowRunCode } from "@/routes/workflows/workflowRun/WorkflowRunCode";
import { WorkflowsPageLayout } from "@/routes/workflows/WorkflowsPageLayout";
import { useTaskV2Query } from "@/routes/runs/useTaskV2Query";

function RunRouter() {
  const { runId } = useParams();

  const { data: task_v2, isLoading } = useTaskV2Query({
    id: runId?.startsWith("tsk_v2") ? runId : undefined,
  });

  const runType = runId?.startsWith("tsk_v2")
    ? "redirect"
    : runId?.startsWith("wr_")
      ? "workflow"
      : runId?.startsWith("tsk_")
        ? "task"
        : null;

  const routes = useMemo(() => {
    if (runType === "workflow") {
      return (
        <Routes>
          <Route element={<WorkflowsPageLayout />}>
            <Route element={<WorkflowRun />}>
              <Route index element={<Navigate to="overview" replace />} />
              <Route
                path="blocks"
                element={<Navigate to="overview" replace />}
              />
              <Route path="overview" element={<WorkflowRunOverview />} />
              <Route path="output" element={<WorkflowRunOutput />} />
              <Route
                path="parameters"
                element={<WorkflowPostRunParameters />}
              />
              <Route path="recording" element={<WorkflowRunRecording />} />
              <Route
                path="code"
                element={<WorkflowRunCode showCacheKeyValueSelector={true} />}
              />
            </Route>
          </Route>
        </Routes>
      );
    }

    if (runType === "task") {
      return (
        <Routes>
          <Route element={<PageLayout />}>
            <Route element={<TaskDetails />}>
              <Route index element={<Navigate to="actions" replace />} />
              <Route path="actions" element={<TaskActions />} />
              <Route path="recording" element={<TaskRecording />} />
              <Route path="parameters" element={<TaskParameters />} />
              <Route path="diagnostics" element={<StepArtifactsLayout />} />
            </Route>
          </Route>
        </Routes>
      );
    }

    return <Status404 />;
  }, [runType]);

  if (runId?.startsWith("tsk_v2")) {
    if (isLoading) {
      return <div>Fetching task details...</div>;
    }

    if (!task_v2) {
      console.error("Task for %s not found", runId);
      return <Status404 />;
    }

    const workflowRunId = task_v2.workflow_run_id;

    if (!workflowRunId) {
      console.error("Workflow run ID for Task V2 %s not found", runId);
      return <Status404 />;
    }

    return <Navigate to={`/runs/${workflowRunId}`} replace />;
  }

  return routes;
}

export { RunRouter };
