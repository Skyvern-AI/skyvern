import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getSampleForInitialFormValues } from "../data/sampleTaskData";
import { SampleCase, sampleCases } from "../types";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import { SavedTaskForm } from "./SavedTaskForm";
import { WorkflowParameter } from "@/api/types";

function CreateNewTaskFormPage() {
  const { template } = useParams();
  const credentialGetter = useCredentialGetter();

  const { data, isFetching } = useQuery({
    queryKey: ["workflows", template],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${template}`)
        .then((response) => response.data);
    },
    enabled: !!template && !sampleCases.includes(template as SampleCase),
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });

  if (!template) {
    return <div>Invalid template</div>;
  }

  if (sampleCases.includes(template as SampleCase)) {
    return (
      <CreateNewTaskForm
        key={template}
        initialValues={getSampleForInitialFormValues(template as SampleCase)}
      />
    );
  }

  if (isFetching) {
    return <div>Loading...</div>;
  }

  const navigationPayload = data.workflow_definition.parameters.find(
    (parameter: WorkflowParameter) => parameter.key === "navigation_payload",
  ).default_value;

  const dataSchema = data.workflow_definition.blocks[0].data_schema;

  const maxSteps = data.workflow_definition.blocks[0].max_steps_per_run;

  return (
    <SavedTaskForm
      initialValues={{
        title: data.title,
        description: data.description,
        webhookCallbackUrl: data.webhook_callback_url,
        proxyLocation: data.proxy_location,
        url: data.workflow_definition.blocks[0].url,
        navigationGoal: data.workflow_definition.blocks[0].navigation_goal,
        dataExtractionGoal:
          data.workflow_definition.blocks[0].data_extraction_goal,
        extractedInformationSchema: JSON.stringify(dataSchema, null, 2),
        navigationPayload,
        maxSteps,
      }}
    />
  );
}

export { CreateNewTaskFormPage };
