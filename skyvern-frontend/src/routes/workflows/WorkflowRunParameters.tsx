import { getClient } from "@/api/AxiosClient";
import { WorkflowApiResponse, WorkflowParameterValueType } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
import { RunWorkflowForm } from "./RunWorkflowForm";

function defaultValue(type: WorkflowParameterValueType) {
  switch (type) {
    case "string":
      return "";
    case "integer":
      return 0;
    case "float":
      return 0.0;
    case "boolean":
      return false;
    case "json":
      return null;
    case "file_url":
      return null;
  }
}

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
  });

  const workflowParameters = workflow?.workflow_definition.parameters.filter(
    (parameter) => parameter.parameter_type === "workflow",
  );

  const initialValues = location.state?.data
    ? location.state.data
    : workflowParameters?.reduce(
        (acc, curr) => {
          if (curr.workflow_parameter_type === "json") {
            if (typeof curr.default_value === "string") {
              acc[curr.key] = curr.default_value;
              return acc;
            }
            if (curr.default_value) {
              acc[curr.key] = JSON.stringify(curr.default_value, null, 2);
              return acc;
            }
          }
          if (
            curr.default_value &&
            curr.workflow_parameter_type === "boolean"
          ) {
            acc[curr.key] = Boolean(curr.default_value);
            return acc;
          }
          if (curr.default_value) {
            acc[curr.key] = curr.default_value;
            return acc;
          }
          acc[curr.key] = defaultValue(curr.workflow_parameter_type);
          return acc;
        },
        {} as Record<string, unknown>,
      );

  if (isFetching) {
    return <div>Getting workflow parameters...</div>;
  }

  if (!workflow || !workflowParameters || !initialValues) {
    return <div>Workflow not found</div>;
  }

  return (
    <div className="space-y-8">
      <header className="space-y-5">
        <h1 className="text-3xl">Run Parameters</h1>
        <h2 className="text-lg text-slate-400">
          Fill the placeholder values that you have linked throughout your
          workflow.
        </h2>
      </header>
      <RunWorkflowForm
        initialValues={initialValues}
        workflowParameters={workflowParameters}
      />
    </div>
  );
}

export { WorkflowRunParameters };
