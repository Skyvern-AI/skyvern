import { useParams } from "react-router-dom";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import { getSampleForInitialFormValues } from "../data/sampleTaskData";
import { SampleCase, sampleCases } from "../types";
import { SavedTaskForm } from "./SavedTaskForm";
import { useQuery } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";

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
        extractedInformationSchema:
          data.workflow_definition.blocks[0].data_schema,
        navigationPayload: data.workflow_definition.parameters[0].default_value,
      }}
    />
  );
}

export { CreateNewTaskFormPage };
