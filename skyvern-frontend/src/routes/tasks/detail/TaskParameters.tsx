import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { KeyValueInput } from "@/components/KeyValueInput";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "@/routes/workflows/editor/nodes/Taskv2Node/types";
import { useQuery } from "@tanstack/react-query";
import { useFirstParam } from "@/hooks/useFirstParam";

function TaskParameters() {
  const taskId = useFirstParam("taskId", "runId");
  const credentialGetter = useCredentialGetter();
  const {
    data: task,
    isLoading: taskIsLoading,
    isError: taskIsError,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
  });

  if (taskIsLoading) {
    return (
      <div className="h-[40rem]">
        <Skeleton className="h-full" />
      </div>
    );
  }

  if (taskIsError || !task) {
    return <div>Error loading parameters</div>;
  }

  return (
    <section className="space-y-8 rounded-lg bg-slate-elevation3 px-6 py-5">
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">URL</h1>
          <h2 className="text-base text-slate-400">
            The starting URL for the task
          </h2>
        </div>
        <Input value={task.request.url} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Navigation Goal</h1>
          <h2 className="text-base text-slate-400">
            Where should Skyvern go and what should Skyvern do?
          </h2>
        </div>
        <AutoResizingTextarea
          value={task.request.navigation_goal ?? ""}
          readOnly
        />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Navigation Payload</h1>
          <h2 className="text-base text-slate-400">
            Specify important parameters, routes, or states
          </h2>
        </div>
        <CodeEditor
          className="w-full"
          language="json"
          value={
            typeof task.request.navigation_payload === "object"
              ? JSON.stringify(task.request.navigation_payload, null, 2)
              : task.request.navigation_payload ?? ""
          }
          readOnly
          minHeight="96px"
          maxHeight="500px"
        />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Data Extraction Goal</h1>
          <h2 className="text-base text-slate-400">
            What outputs are you looking to get?
          </h2>
        </div>
        <AutoResizingTextarea
          value={task.request.data_extraction_goal ?? ""}
          readOnly
        />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Data Schema</h1>
          <h2 className="text-base text-slate-400">
            Specify the output format in JSON
          </h2>
        </div>
        <CodeEditor
          className="w-full"
          language="json"
          value={
            typeof task.request.extracted_information_schema === "object"
              ? JSON.stringify(
                  task.request.extracted_information_schema,
                  null,
                  2,
                )
              : task.request.extracted_information_schema ?? ""
          }
          readOnly
          minHeight="96px"
          maxHeight="500px"
        />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Extra HTTP Headers</h1>
          <h2 className="text-base text-slate-400">
            Specify some self-defined HTTP requests headers
          </h2>
        </div>
        <div className="w-full">
          <KeyValueInput
            value={
              task.request.extra_http_headers
                ? JSON.stringify(task.request.extra_http_headers)
                : null
            }
            readOnly={true}
            onChange={() => {}}
          />
        </div>
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Webhook Callback URL</h1>
          <h2 className="text-base text-slate-400">
            The URL of a webhook endpoint to send the extracted information
          </h2>
        </div>
        <Input value={task.request.webhook_callback_url ?? ""} readOnly />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Max Screenshot Scrolls</h1>
          <h2 className="text-base text-slate-400">
            The maximum number of times to scroll the page
          </h2>
        </div>
        <Input
          placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
          value={task.request.max_screenshot_scrolls ?? ""}
          readOnly
        />
      </div>
      <div className="flex gap-16">
        <div className="w-72">
          <h1 className="text-lg">Include Action History</h1>
          <h2 className="text-base text-slate-400">
            Include the action history in the completion verification
          </h2>
        </div>
        <div className="w-full">
          <Switch
            checked={
              task.request.include_action_history_in_verification ?? false
            }
            disabled
          />
        </div>
      </div>
    </section>
  );
}

export { TaskParameters };
