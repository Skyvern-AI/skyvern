export type RunViewRouteParams = Readonly<{
  runId?: string;
  taskId?: string;
  workflowPermanentId?: string;
  workflowRunId?: string;
}>;

export function runViewTabBasePath(
  params: RunViewRouteParams,
): string | undefined {
  if (params.workflowPermanentId && params.workflowRunId) {
    return `/agents/${params.workflowPermanentId}/${params.workflowRunId}`;
  }
  if (params.taskId) {
    return `/tasks/${params.taskId}`;
  }
  if (params.runId) {
    return `/runs/${params.runId}`;
  }
  return undefined;
}
