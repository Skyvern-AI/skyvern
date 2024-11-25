import { getClient } from "@/api/AxiosClient";
import { TaskGenerationApiResponse } from "@/api/types";
import img from "@/assets/promptBoxBg.png";
import { BookIcon } from "@/components/icons/BookIcon";
import { CartIcon } from "@/components/icons/CartIcon";
import { GraphIcon } from "@/components/icons/GraphIcon";
import { InboxIcon } from "@/components/icons/InboxIcon";
import { MessageIcon } from "@/components/icons/MessageIcon";
import { TranslateIcon } from "@/components/icons/TranslateIcon";
import { TrophyIcon } from "@/components/icons/TrophyIcon";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  FileTextIcon,
  GearIcon,
  PaperPlaneIcon,
  Pencil1Icon,
  ReloadIcon,
} from "@radix-ui/react-icons";
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
    label: "Add a product to cart",
    icon: <CartIcon className="size-6" />,
  },
  {
    key: "geico",
    label: "Get an insurance quote",
    icon: <FileTextIcon className="size-6" />,
  },
  {
    key: "job_application",
    label: "Apply for a job",
    icon: <InboxIcon className="size-6" />,
  },
  {
    key: "california_edd",
    label: "Fill out CA's online EDD",
    icon: <Pencil1Icon className="size-6" />,
  },
  {
    key: "contact_us_forms",
    label: "Fill a contact us form",
    icon: <FileTextIcon className="size-6" />,
  },
  {
    key: "bci_seguros",
    label: "Get an auto insurance quote in spanish",
    icon: <TranslateIcon className="size-6" />,
  },
  {
    key: "hackernews",
    label: "What's the top post on hackernews",
    icon: <MessageIcon className="size-6" />,
  },
  {
    key: "AAPLStockPrice",
    label: "Search for AAPL on Google Finance",
    icon: <GraphIcon className="size-6" />,
  },
  {
    key: "NYTBestseller",
    label: "Get the top NYT bestseller",
    icon: <BookIcon className="size-6" />,
  },
  {
    key: "topRankedFootballTeam",
    label: "Get the top ranked football team",
    icon: <TrophyIcon className="size-6" />,
  },
  {
    key: "extractIntegrationsFromGong",
    label: "Extract Integrations from Gong.io",
    icon: <GearIcon className="size-6" />,
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
        <div className="mx-auto flex min-w-44 flex-col items-center gap-7 px-8">
          <span className="text-2xl">
            What task would you like to accomplish?
          </span>
          <div className="flex w-full max-w-xl items-center rounded-xl bg-slate-700 py-2 pr-4 lg:w-3/4">
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
              className="flex cursor-pointer gap-2 whitespace-normal rounded-sm bg-slate-elevation3 px-4 py-3 hover:bg-slate-elevation5 lg:whitespace-nowrap"
              onClick={() => {
                navigate(`/create/${example.key}`);
              }}
            >
              <div>{example.icon}</div>
              <div>{example.label}</div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { PromptBox };
