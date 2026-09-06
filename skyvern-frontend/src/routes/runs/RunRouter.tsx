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

import { LogoMinimized } from "@/components/LogoMinimized";
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
import { useWorkflowStudioFlagState } from "@/hooks/useWorkflowStudioEnabled";
import { useTaskV2Query } from "@/routes/runs/useTaskV2Query";
import {
  SYSTEM_RUN_FOCUS_PARAM,
  toReadableSearch,
} from "@/routes/workflows/studio/panes";

// Sub-paths the ?wr= redirect below may forward; anything else (including the
// self-redirecting blocks route and crafted splats) lands on overview.
const REDIRECTABLE_RUN_SUBPATHS = new Set([
  "overview",
  "output",
  "parameters",
  "recording",
  "code",
]);

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
  const { runId, "*": subPath } = useParams();
  const studioFlagState = useWorkflowStudioFlagState();
  const studioEnabled = studioFlagState ?? false;

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
                element={<Navigate to={`/runs/${runId}/overview`} replace />}
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
  }, [runType, runId]);

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

  if (renderStudioRun) {
    if (!studioRun) {
      // A permanently failed initial fetch (deleted, foreign-org, or garbage run
      // id) lands on 404 like the legacy run view; a failed background poll of a
      // live run retains its data, so it never flashes 404 over a working view.
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

  // A studio URL carries the viewed run in ?wr= and a possibly stale one in the
  // path, so honor ?wr= here — but only once the flag is known off (or under
  // ?embed=true, which never renders the studio), since rewriting while flags
  // resolve would strip a studio user's URL state. Land on the sub-path
  // directly because the index and /blocks redirects drop the search (?active=
  // must survive), and scrub ?wrs=/?bl=, studio-internal companions of ?wr=.
  const sharedStudioRunId = searchParams.get("wr");
  if (
    runType === "workflow" &&
    (studioFlagState === false || isEmbedded) &&
    sharedStudioRunId !== null &&
    /^wr_\w+$/.test(sharedStudioRunId) &&
    sharedStudioRunId !== runId
  ) {
    const params = new URLSearchParams(searchParams);
    params.delete("wr");
    params.delete(SYSTEM_RUN_FOCUS_PARAM);
    params.delete("bl");
    const targetSubPath =
      subPath && REDIRECTABLE_RUN_SUBPATHS.has(subPath) ? subPath : "overview";
    return (
      <Navigate
        to={`/runs/${sharedStudioRunId}/${targetSubPath}${toReadableSearch(params)}`}
        replace
      />
    );
  }

  return routes;
}

export { RunRouter };
