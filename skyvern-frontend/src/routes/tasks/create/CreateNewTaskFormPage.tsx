import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useLocation, useParams } from "react-router-dom";
import { getSampleForInitialFormValues } from "../data/sampleTaskData";
import { SampleCase, sampleCases } from "../types";
import { CreateNewTaskForm } from "./CreateNewTaskForm";
import { SavedTaskForm } from "./SavedTaskForm";
import { TaskGenerationApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

function CreateNewTaskFormPage() {
  const { template } = useParams();
  const credentialGetter = useCredentialGetter();
  const location = useLocation();

  const { data, isFetching } = useQuery({
    queryKey: ["savedTask", template],
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

  if (template === "from-prompt") {
    const data = location.state?.data as TaskGenerationApiResponse;
    if (!data.url) {
      return <div>Something went wrong, please try again</div>; // this should never happen
    }
    return (
      <div className="space-y-4">
        <header>
          <h1 className="text-3xl">Create New Task</h1>
        </header>
        <CreateNewTaskForm
          key={template}
          initialValues={{
            url: data.url,
            navigationGoal: data.navigation_goal,
            dataExtractionGoal: data.data_extraction_goal,
            navigationPayload:
              typeof data.navigation_payload === "string"
                ? data.navigation_payload
                : JSON.stringify(data.navigation_payload, null, 2),
            extractedInformationSchema: JSON.stringify(
              data.extracted_information_schema,
              null,
              2,
            ),
            errorCodeMapping: null,
            totpIdentifier: null,
            totpVerificationUrl: null,
            webhookCallbackUrl: null,
            proxyLocation: null,
          }}
        />
      </div>
    );
  }

  if (sampleCases.includes(template as SampleCase)) {
    return (
      <div className="space-y-4">
        <header>
          <h1 className="text-3xl">Create New Task</h1>
        </header>
        <CreateNewTaskForm
          key={template}
          initialValues={getSampleForInitialFormValues(template as SampleCase)}
        />
      </div>
    );
  }

  if (isFetching) {
    return (
      <div className="space-y-4">
        <header>
          <h1 className="text-3xl">Edit Task Template</h1>
        </header>
        <Skeleton className="h-96" />
        <Skeleton className="h-20" />
        <Skeleton className="h-20" />
      </div>
    );
  }

  const navigationPayload = data.workflow_definition.parameters.find(
    (parameter: WorkflowParameter) => parameter.key === "navigation_payload",
  ).default_value;

  const dataSchema = data.workflow_definition.blocks[0].data_schema;
  const errorCodeMapping =
    data.workflow_definition.blocks[0].error_code_mapping;

  const maxStepsOverride = data.workflow_definition.blocks[0].max_steps_per_run;

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-3xl">Edit Task Template</h1>
      </header>
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
          maxStepsOverride,
          totpIdentifier: data.workflow_definition.blocks[0].totp_identifier,
          totpVerificationUrl:
            data.workflow_definition.blocks[0].totp_verification_url,
          errorCodeMapping: JSON.stringify(errorCodeMapping, null, 2),
        }}
      />
    </div>
  );
}

export { CreateNewTaskFormPage };
