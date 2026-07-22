/**
 * A router component that handles both workflow runs (wr_xxx) and task runs (tsk_xxx)
 * under the /runs/:runId path, discriminating based on ID prefix.
 */

import {
  Navigate,
  Route,
  Routes,
  useParams,
  useSearchParams,
} from "react-router-dom";
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
import { WorkflowEditor } from "@/routes/workflows/editor/WorkflowEditor";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useWorkflowRunWithWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { useTaskV2Query } from "@/routes/runs/useTaskV2Query";

function RunRouter() {
  const { runId } = useParams();
  const studioEnabled = useWorkflowStudioEnabled();

  const { data: task_v2, isLoading } = useTaskV2Query({
    id: runId?.startsWith("tsk_v2") ? runId : undefined,
  });

  // With the studio on, a workflow-run short URL renders the studio run view in
  // place (short URL stays in the address bar) rather than redirecting to the
  // long /agents/{wpid}/studio?wr= form. The studio components read the workflow
  // id, which the path lacks, so resolve it from the run first — this query is
  // shared/cached with the shell that renders next. An embedded run (?embed=true)
  // keeps the legacy chrome-free view instead of the full studio shell.
  const [searchParams] = useSearchParams();
  const isEmbedded = searchParams.get("embed") === "true";
  const renderStudioRun =
    studioEnabled && Boolean(runId?.startsWith("wr_")) && !isEmbedded;
  const { data: studioRun, isError: studioRunFailed } =
    useWorkflowRunWithWorkflowQuery({
      workflowRunId: renderStudioRun ? runId : undefined,
      enabled: renderStudioRun,
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

  if (renderStudioRun) {
    // keepPreviousData holds the prior run's response while navigating between
    // short URLs; wait for the fetch that matches this runId before handing its
    // workflow id to the studio, so the editor and run panes never mix two runs.
    const resolvedRun =
      studioRun?.workflow_run_id === runId ? studioRun : undefined;
    if (!resolvedRun) {
      // No matching run data yet. A permanently failed initial fetch (deleted,
      // foreign-org, or garbage run id) lands on 404 like the legacy run view; a
      // failed background poll of a live run retains its data, so resolvedRun
      // stays set above and never flashes 404 over a working view.
      if (studioRunFailed) {
        return <Status404 />;
      }
      return <div>Fetching run details...</div>;
    }
    const workflowPermanentId = resolvedRun.workflow?.workflow_permanent_id;
    if (!workflowPermanentId) {
      console.error("Workflow permanent ID for run %s not found", runId);
      return <Status404 />;
    }
    return (
      <WorkflowPermanentIdContext.Provider value={workflowPermanentId}>
        <WorkflowEditor />
      </WorkflowPermanentIdContext.Provider>
    );
  }

  return routes;
}

export { RunRouter };
