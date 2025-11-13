import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
import { RunWorkflowForm } from "./RunWorkflowForm";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { Skeleton } from "@/components/ui/skeleton";
import { ProxyLocation } from "@/api/types";
import { getInitialValues } from "./utils";

function WorkflowRunParameters() {
  const credentialGetter = useCredentialGetter();
  const { workflowPermanentId } = useParams();
  const location = useLocation();

  const { data: workflow, isFetching } = useQuery<WorkflowApiResponse>({
    queryKey: ["workflow", workflowPermanentId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${workflowPermanentId}`)
        .then((response) => response.data);
    },
    refetchOnWindowFocus: false,
  });

  const workflowParameters = workflow?.workflow_definition.parameters.filter(
    (parameter) => parameter.parameter_type === "workflow",
  );

  const proxyLocation = location.state
    ? (location.state.proxyLocation as ProxyLocation)
    : null;

  const maxScreenshotScrolls = location.state?.maxScreenshotScrolls ?? null;

  const webhookCallbackUrl = location.state
    ? (location.state.webhookCallbackUrl as string)
    : null;

  const extraHttpHeaders = location.state
    ? (location.state.extraHttpHeaders as Record<string, string>)
    : null;

  const initialValues = getInitialValues(location, workflowParameters ?? []);

  const header = (
    <header className="space-y-5">
      <h1 className="text-3xl">
        Parameters{workflow?.title ? ` - ${workflow.title}` : ""}
      </h1>
      <h2 className="text-lg text-slate-400">
        Fill the placeholder values that you have linked throughout your
        workflow.
      </h2>
    </header>
  );

  if (isFetching) {
    return (
      <div className="space-y-8">
        {header}
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!workflow || !workflowParameters || !initialValues) {
    return <div>Workflow not found</div>;
  }

  return (
    <div className="space-y-8">
      {header}
      <RunWorkflowForm
        initialValues={initialValues}
        workflowParameters={workflowParameters}
        initialSettings={{
          proxyLocation:
            proxyLocation ??
            workflow.proxy_location ??
            ProxyLocation.Residential,
          webhookCallbackUrl:
            webhookCallbackUrl ?? workflow.webhook_callback_url ?? "",
          maxScreenshotScrolls:
            maxScreenshotScrolls ?? workflow.max_screenshot_scrolls ?? null,
          extraHttpHeaders:
            extraHttpHeaders ?? workflow.extra_http_headers ?? null,
          cdpAddress: null,
        }}
      />
    </div>
  );
}

export { WorkflowRunParameters };
