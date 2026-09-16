import { Navigate, useLocation, useParams } from "react-router-dom";

import { LegacyBuildRedirect } from "./LegacyBuildRedirect";
import { WorkflowEditor } from "./editor/WorkflowEditor";
import {
  parsePanesParam,
  RUN_APPEND_PANES,
  STUDIO_PANES_PARAM,
  toReadableSearch,
} from "./studio/panes";

export function BuildRoute() {
  return <LegacyBuildRedirect />;
}

export function DebugRoute() {
  return <LegacyBuildRedirect />;
}

export function StudioRoute() {
  return <WorkflowEditor />;
}

export function EditRoute() {
  const { workflowPermanentId } = useParams();
  const location = useLocation();
  return (
    <Navigate
      to={`/agents/${workflowPermanentId}/studio${location.search}`}
      state={location.state}
      replace
    />
  );
}

/**
 * Legacy per-agent run URLs now land on the short run URL, where workflow runs
 * render inside the studio.
 */
export function WorkflowRunRoute() {
  const params = useParams();
  const workflowRunId = params.workflowRunId;
  const location = useLocation();

  if (!workflowRunId) {
    return <Navigate to="/runs" replace />;
  }

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
  if (routedStudioView) {
    searchParams.set("view", routedStudioView);
    const embedded = searchParams.get("embed") === "true";
    const requiredPane =
      routedStudioView === "recording" ? "browser" : "overview";
    const panes = embedded
      ? [requiredPane]
      : (parsePanesParam(searchParams.get(STUDIO_PANES_PARAM)) ?? [
          ...RUN_APPEND_PANES,
        ]);
    searchParams.set(
      STUDIO_PANES_PARAM,
      embedded
        ? requiredPane
        : [requiredPane, ...panes.filter((pane) => pane !== requiredPane)].join(
            ",",
          ),
    );
  }

  return (
    <Navigate
      to={{
        pathname: `/runs/${encodeURIComponent(workflowRunId)}`,
        search: toReadableSearch(searchParams),
        hash: location.hash,
      }}
      state={location.state}
      replace
    />
  );
}
