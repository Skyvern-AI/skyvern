import { useParams } from "react-router-dom";
import { useTaskQuery } from "../../detail/hooks/useTaskQuery";
import { CreateNewTaskForm } from "../CreateNewTaskForm";

function RetryTask() {
  const { taskId } = useParams();
  const { data: task, isLoading } = useTaskQuery({ id: taskId });

  if (isLoading) {
    return <div>Fetching task details...</div>;
  }

  if (!task) {
    return null;
  }

  return (
    <CreateNewTaskForm
      initialValues={{
        url: task.request.url,
        navigationGoal: task.request.navigation_goal,
        navigationPayload:
          typeof task.request.navigation_payload === "string"
            ? task.request.navigation_payload
            : JSON.stringify(task.request.navigation_payload, null, 2),
        dataExtractionGoal: task.request.data_extraction_goal,
        extractedInformationSchema:
          typeof task.request.extracted_information_schema === "string"
            ? task.request.extracted_information_schema
            : JSON.stringify(
                task.request.extracted_information_schema,
                null,
                2,
              ),
        webhookCallbackUrl: task.request.webhook_callback_url,
        totpIdentifier: task.request.totp_identifier,
        totpVerificationUrl: task.request.totp_verification_url,
      }}
    />
  );
}

export { RetryTask };
