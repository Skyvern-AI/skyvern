/**
 * A router component that handles both workflow runs (wr_xxx) and task runs (tsk_xxx)
 * under the /runs/:runId path, discriminating based on ID prefix.
 */

import {
  Navigate,
  Route,
  Routes,
  useLocation,
  useParams,
} from "react-router-dom";
import { useMemo } from "react";

import { LogoMinimized } from "@/components/LogoMinimized";
import { PageLayout } from "@/components/PageLayout";
import { Status404 } from "@/components/Status404";
import { StepArtifactsLayout } from "@/routes/tasks/detail/StepArtifactsLayout";
import { TaskActions } from "@/routes/tasks/detail/TaskActions";
import { TaskDetails } from "@/routes/tasks/detail/TaskDetails";
import { TaskParameters } from "@/routes/tasks/detail/TaskParameters";
import { TaskRecording } from "@/routes/tasks/detail/TaskRecording";
import { WorkflowEditor } from "@/routes/workflows/editor/WorkflowEditor";
import { WorkflowPermanentIdContext } from "@/routes/workflows/WorkflowPermanentIdContext";
import { useWorkflowRunWithWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowRunWithWorkflowQuery";
import { toReadableSearch } from "@/routes/workflows/studio/panes";
import { useTaskV2Query } from "@/routes/runs/useTaskV2Query";

const loadingIndicator = (
  <div
    className="flex h-screen w-full items-center justify-center"
    role="status"
  >
    <div className="animate-pulse">
      <LogoMinimized />
    </div>
    <span className="sr-only">Loading</span>
  </div>
);

function RunRouter() {
  const params = useParams();
  const runId = params.runId;
  const location = useLocation();
  const isTaskV2Run = Boolean(runId?.startsWith("tsk_v2"));
  const isWorkflowRun = Boolean(runId?.startsWith("wr_"));

  const { data: task_v2, isLoading } = useTaskV2Query({
    id: isTaskV2Run ? runId : undefined,
  });

  // Workflow-run short URLs render the studio run view in place (short URL
  // stays in the address bar) rather than redirecting to the long
  // /agents/{wpid}/studio?wr= form. The studio components read the workflow id,
  // which the path lacks, so resolve it from the run first — this query is
  // shared/cached with the shell that renders next.
  const { data: studioRun, isError: studioRunFailed } =
    useWorkflowRunWithWorkflowQuery({
      workflowRunId: isWorkflowRun ? runId : undefined,
      enabled: isWorkflowRun,
    });

  const runType = isTaskV2Run
    ? "redirect"
    : isWorkflowRun
      ? "workflow"
      : runId?.startsWith("tsk_")
        ? "task"
        : null;

  const routes = useMemo(() => {
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

  const legacySubview = params["*"]?.split("/")[0] || undefined;
  const studioView = legacySubview
    ? {
        overview: "timeline",
        blocks: "timeline",
        output: "outputs",
        parameters: "inputs",
        recording: "recording",
        code: "code",
      }[legacySubview]
    : undefined;
  const searchParams = new URLSearchParams(location.search);
  const routedStudioView =
    searchParams.has("active") &&
    (studioView === "outputs" || studioView === "inputs")
      ? "timeline"
      : studioView;
  if (isWorkflowRun && routedStudioView) {
    searchParams.set("view", routedStudioView);
    return (
      <Navigate
        to={{
          pathname: `/runs/${runId}`,
          search: toReadableSearch(searchParams),
          hash: location.hash,
        }}
        replace
      />
    );
  }

  if (runId?.startsWith("tsk_v2")) {
    if (isLoading) {
      return loadingIndicator;
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

  if (isWorkflowRun) {
    if (!studioRun) {
      // A permanently failed initial fetch (foreign-org or garbage run id)
      // lands on 404; a failed background poll of a live run retains its data,
      // so it never flashes 404 over a working view.
      if (studioRunFailed) {
        return <Status404 />;
      }
      return loadingIndicator;
    }
    const workflowPermanentId = studioRun.workflow?.workflow_permanent_id;
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
