import * as env from "@/util/env";

/**
 * Where "open the editor" links point. With the studio preview on, the editor
 * lives at /studio; off, it is the legacy /build debugger surface.
 */
export function workflowEditorPath(
  workflowPermanentId: string,
  studioEnabled: boolean,
  search = "",
): string {
  const leaf = studioEnabled ? "studio" : "build";
  return `/agents/${workflowPermanentId}/${leaf}${search}`;
}

/**
 * Legacy (studio-off) destination for viewing a finished workflow run, honoring
 * the existing useNewRunsUrl split between the global and per-workflow pages.
 */
export function legacyRunDetailPath(
  workflowPermanentId: string,
  workflowRunId: string,
): string {
  return env.useNewRunsUrl
    ? `/runs/${workflowRunId}`
    : `/agents/${workflowPermanentId}/${workflowRunId}/overview`;
}
