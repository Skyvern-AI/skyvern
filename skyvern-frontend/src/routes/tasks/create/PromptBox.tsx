import { getClient } from "@/api/AxiosClient";
import { TaskGenerationApiResponse } from "@/api/types";
import img from "@/assets/promptBoxBg.png";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { PaperPlaneIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";

function createTemplateTaskFromTaskGenerationParameters(
  values: TaskGenerationApiResponse,
) {
  return {
    title: values.suggested_title ?? "Untitled Task",
    description: "",
    is_saved_task: true,
    webhook_callback_url: null,
    proxy_location: "RESIDENTIAL",
    workflow_definition: {
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "json",
          key: "navigation_payload",
          default_value: JSON.stringify(values.navigation_payload),
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: values.suggested_title ?? "Untitled Task",
          url: values.url,
          navigation_goal: values.navigation_goal,
          data_extraction_goal: values.data_extraction_goal,
          data_schema: values.extracted_information_schema,
        },
      ],
    },
  };
}

const exampleCases = [
  {
    key: "finditparts",
    label: "Find a product and add it to cart",
  },
  {
    key: "geico",
    label: "Generate an insurance quote",
  },
  {
    key: "job_application",
    label: "Apply to a job",
  },
  {
    key: "california_edd",
    label: "Fill an online enrollment form",
  },
  {
    key: "contact_us_forms",
    label: "Fill a contact us form",
  },
  {
    key: "bci_seguros",
    label: "Generate an auto insurance quote in spanish",
  },
  {
    key: "hackernews",
    label: "Get the top post on Hackernews",
  },
  {
    key: "AAPLStockPrice",
    label: "Get the stock price of AAPL",
  },
  {
    key: "NYTBestseller",
    label: "Get the top NYT bestseller",
  },
  {
    key: "topRankedFootballTeam",
    label: "Get the top ranked football team",
  },
];

function PromptBox() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState<string>("");
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const getTaskFromPromptMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter);
      return client
        .post<
          { prompt: string },
          { data: TaskGenerationApiResponse }
        >("/generate/task", { prompt })
        .then((response) => response.data);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error creating task from prompt",
        description: error.message,
      });
    },
  });

  const saveTaskMutation = useMutation({
    mutationFn: async (params: TaskGenerationApiResponse) => {
      const client = await getClient(credentialGetter);
      const templateTask =
        createTemplateTaskFromTaskGenerationParameters(params);
      const yaml = convertToYAML(templateTask);
      return client.post("/workflows", yaml, {
        headers: {
          "Content-Type": "text/plain",
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["savedTasks"],
      });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error saving task",
        description: error.message,
      });
    },
  });

  return (
    <div>
      <div
        className="rounded-sm py-[4.25rem]"
        style={{
          background: `url(${img}) 50% / cover no-repeat`,
        }}
      >
        <div className="flex flex-col items-center gap-7">
          <span className="text-2xl">
            What task would you like to accomplish?
          </span>
          <div className="flex w-1/2 max-w-xl items-center rounded-xl bg-slate-700 py-2 pr-4">
            <Textarea
              className="min-h-0 resize-none rounded-xl border-transparent px-4 hover:border-transparent focus-visible:ring-0"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Enter your prompt..."
              rows={1}
            />
            <div className="h-full">
              {getTaskFromPromptMutation.isPending ||
              saveTaskMutation.isPending ? (
                <ReloadIcon className="h-6 w-6 animate-spin" />
              ) : (
                <PaperPlaneIcon
                  className="h-6 w-6 cursor-pointer"
                  onClick={async () => {
                    const taskGenerationResponse =
                      await getTaskFromPromptMutation.mutateAsync(prompt);
                    await saveTaskMutation.mutateAsync(taskGenerationResponse);
                    navigate("/create/from-prompt", {
                      state: {
                        data: taskGenerationResponse,
                      },
                    });
                  }}
                />
              )}
            </div>
          </div>
        </div>
      </div>
      <div className="flex flex-wrap justify-center gap-4 rounded-sm bg-slate-elevation1 p-4">
        {exampleCases.map((example) => {
          return (
            <div
              key={example.key}
              className="cursor-pointer whitespace-nowrap rounded-sm bg-slate-elevation3 px-4 py-3 hover:bg-slate-elevation5"
              onClick={() => {
                navigate(`/create/${example.key}`);
              }}
            >
              {example.label}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { PromptBox };
