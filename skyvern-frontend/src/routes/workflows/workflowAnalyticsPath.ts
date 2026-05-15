function buildWorkflowAnalyticsPath(workflowPermanentId: string): string {
  const params = new URLSearchParams();
  params.set("compare", workflowPermanentId);
  return `/analytics?${params.toString()}`;
}

export { buildWorkflowAnalyticsPath };
