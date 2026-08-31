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

export function runOverviewScreenshotLocation(
  runBasePath: string,
  search: string,
  workflowRunBlockId: string,
) {
  const params = new URLSearchParams(search);
  params.set("active", workflowRunBlockId);
  params.delete("iteration");
  const nextSearch = params.toString();
  return {
    pathname: `${runBasePath}/overview`,
    search: nextSearch ? `?${nextSearch}` : "",
  };
}
