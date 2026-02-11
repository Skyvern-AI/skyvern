import { useTaskQuery } from "../../detail/hooks/useTaskQuery";
import { CreateNewTaskForm } from "../CreateNewTaskForm";
import { useFirstParam } from "@/hooks/useFirstParam";

function RetryTask() {
  const taskId = useFirstParam("taskId", "runId");
  const { data: task, isLoading } = useTaskQuery({ id: taskId });

  if (isLoading) {
    return <div>Fetching task details...</div>;
  }

  if (!task) {
    return null;
  }

  return (
    <div className="space-y-4">
      <header>
        <h1 className="text-3xl">Rerun Task</h1>
      </header>
      <CreateNewTaskForm
        initialValues={{
          url: task.request.url,
          navigationGoal: task.request.navigation_goal ?? null,
          navigationPayload:
            typeof task.request.navigation_payload === "string"
              ? task.request.navigation_payload
              : JSON.stringify(task.request.navigation_payload, null, 2),
          dataExtractionGoal: task.request.data_extraction_goal ?? null,
          extractedInformationSchema:
            typeof task.request.extracted_information_schema === "string"
              ? task.request.extracted_information_schema
              : JSON.stringify(
                  task.request.extracted_information_schema,
                  null,
                  2,
                ),
          webhookCallbackUrl: task.request.webhook_callback_url ?? null,
          totpIdentifier: task.request.totp_identifier ?? null,
          errorCodeMapping: task.request.error_code_mapping
            ? JSON.stringify(task.request.error_code_mapping, null, 2)
            : "",
          proxyLocation: task.request.proxy_location ?? null,
          includeActionHistoryInVerification:
            task.request.include_action_history_in_verification ?? false,
          maxScreenshotScrolls: task.request.max_screenshot_scrolls ?? null,
          extraHttpHeaders: task.request.extra_http_headers
            ? JSON.stringify(task.request.extra_http_headers)
            : null,
          cdpAddress: task.request.browser_address ?? null,
        }}
      />
    </div>
  );
}

export { RetryTask };
