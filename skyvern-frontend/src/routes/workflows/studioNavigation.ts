/**
 * Where "open the editor" links point.
 */
export function workflowEditorPath(
  workflowPermanentId: string,
  _studioEnabled: boolean,
  search = "",
): string {
  return `/agents/${workflowPermanentId}/studio${search}`;
}

export function workflowRunDetailPath(workflowRunId: string): string {
  return `/runs/${workflowRunId}`;
}
