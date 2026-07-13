import { Navigate, useLocation, useParams } from "react-router-dom";

import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";

import { DebugToBuildRedirect } from "./DebugToBuildRedirect";
import { LegacyBuildRedirect } from "./LegacyBuildRedirect";
import { Debugger } from "./debugger/Debugger";
import { WorkflowEditor } from "./editor/WorkflowEditor";

/**
 * /build (and block-build) routes. With the studio preview on, these redirect
 * into the editor; off, they render the legacy Debugger.
 */
export function BuildRoute() {
  const studioEnabled = useWorkflowStudioEnabled();
  return studioEnabled ? <LegacyBuildRedirect /> : <Debugger />;
}

/**
 * /debug (and block-debug) routes. With the studio preview on, these redirect
 * into the editor; off, they fall back to the legacy debug→build redirect.
 */
export function DebugRoute() {
  const studioEnabled = useWorkflowStudioEnabled();
  return studioEnabled ? <LegacyBuildRedirect /> : <DebugToBuildRedirect />;
}

/**
 * /studio — the studio shell when the preview is on; off, bounce to the legacy
 * /edit editor so a shared studio link never strands a non-flagged user.
 */
export function StudioRoute() {
  const studioEnabled = useWorkflowStudioEnabled();
  const { workflowPermanentId } = useParams();
  const location = useLocation();
  if (studioEnabled) {
    return <WorkflowEditor />;
  }
  return (
    <Navigate
      to={`/agents/${workflowPermanentId}/edit${location.search}`}
      state={location.state}
      replace
    />
  );
}

/**
 * /edit — the legacy editor when the preview is off; on, the studio lives at
 * /studio, so redirect there (preserving query + state) for old links.
 */
export function EditRoute() {
  const studioEnabled = useWorkflowStudioEnabled();
  const { workflowPermanentId } = useParams();
  const location = useLocation();
  if (studioEnabled) {
    return (
      <Navigate
        to={`/agents/${workflowPermanentId}/studio${location.search}`}
        state={location.state}
        replace
      />
    );
  }
  return <WorkflowEditor />;
}
