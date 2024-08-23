import { SampleCase } from "../types";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { useNavigate } from "react-router-dom";
import { SavedTasks } from "./SavedTasks";
import { getSample } from "../data/sampleTaskData";
import { Textarea } from "@/components/ui/textarea";
import { useState } from "react";
import {
  InfoCircledIcon,
  PaperPlaneIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { AxiosError } from "axios";
import { toast } from "@/components/ui/use-toast";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { TaskGenerationApiResponse } from "@/api/types";
import { stringify as convertToYAML } from "yaml";

const examplePrompts = [
  "What is the top post on hackernews?",
  "Navigate to Google Finance and search for AAPL",
];

const templateSamples: {
  [key in SampleCase]: {
    title: string;
    description: string;
  };
} = {
  blank: {
    title: "Blank",
    description: "Create task from a blank template",
  },
  geico: {
    title: "Geico",
    description: "Generate an auto insurance quote",
  },
  finditparts: {
    title: "Finditparts",
    description: "Find a product and add it to cart",
  },
  california_edd: {
    title: "California_EDD",
    description: "Fill the employer services online enrollment form",
  },
  bci_seguros: {
    title: "bci_seguros",
    description: "Generate an auto insurance quote",
  },
  job_application: {
    title: "Job Application",
    description: "Fill a job application form",
  },
};

function createTemplateTaskFromTaskGenerationParameters(
  values: TaskGenerationApiResponse,
) {
  return {
    title: values.suggested_title,
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
          label: values.suggested_title,
          url: values.url,
          navigation_goal: values.navigation_goal,
          data_extraction_goal: values.data_extraction_goal,
          data_schema: values.extracted_information_schema,
        },
      ],
    },
  };
}

function createTaskFromTaskGenerationParameters(
  values: TaskGenerationApiResponse,
) {
  return {
    url: values.url,
    navigation_goal: values.navigation_goal,
    data_extraction_goal: values.data_extraction_goal,
    proxy_location: "RESIDENTIAL",
    navigation_payload: values.navigation_payload,
    extracted_information_schema: values.extracted_information_schema,
  };
}

function TaskTemplates() {
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
        title: "Error creating task from prompt",
        description: error.message,
      });
    },
  });

  const runTaskMutation = useMutation({
    mutationFn: async (params: TaskGenerationApiResponse) => {
      const client = await getClient(credentialGetter);
      const data = createTaskFromTaskGenerationParameters(params);
      return client.post<
        ReturnType<typeof createTaskFromTaskGenerationParameters>,
        { data: { task_id: string } }
      >("/tasks", data);
    },
    onSuccess: (response) => {
      navigate(`/tasks/${response.data.task_id}/actions`);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error creating task from prompt",
        description: error.message,
      });
    },
  });

  return (
    <div>
      <Alert variant="warning">
        <InfoCircledIcon className="h-4 w-4" />
        <AlertTitle>
          Have a complicated workflow you would like to automate?
        </AlertTitle>
        <AlertDescription>
          <a
            href="https://meetings.hubspot.com/suchintan"
            target="_blank"
            rel="noopener noreferrer"
            className="ml-auto underline underline-offset-2"
          >
            Book a demo {"->"}
          </a>
        </AlertDescription>
      </Alert>
      <section className="py-4">
        <header>
          <h1 className="mb-2 text-3xl">Try a prompt</h1>
        </header>
        <p className="text-sm">
          We will generate a task for you automatically.
        </p>
        <Separator className="mb-8 mt-2" />
        <div className="mx-auto flex max-w-xl items-center rounded-xl border pr-4">
          <Textarea
            className="resize-none rounded-xl border-transparent p-2 font-mono text-sm hover:border-transparent focus-visible:ring-0"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Enter your prompt..."
          />
          <div className="h-full">
            {getTaskFromPromptMutation.isPending ||
            saveTaskMutation.isPending ||
            runTaskMutation.isPending ? (
              <ReloadIcon className="h-6 w-6 animate-spin" />
            ) : (
              <PaperPlaneIcon
                className="h-6 w-6 cursor-pointer"
                onClick={async () => {
                  const taskGenerationResponse =
                    await getTaskFromPromptMutation.mutateAsync(prompt);
                  await saveTaskMutation.mutateAsync(taskGenerationResponse);
                  await runTaskMutation.mutateAsync(taskGenerationResponse);
                }}
              />
            )}
          </div>
        </div>
        <div className="mt-4 flex flex-wrap justify-center gap-4">
          {examplePrompts.map((examplePrompt) => {
            return (
              <div
                key={examplePrompt}
                className="cursor-pointer rounded-xl border p-2 text-sm text-muted-foreground"
                onClick={() => {
                  setPrompt(examplePrompt);
                }}
              >
                {examplePrompt}
              </div>
            );
          })}
        </div>
      </section>
      <section className="py-4">
        <header>
          <h1 className="text-3xl">Your Templates</h1>
        </header>
        <p className="mt-1 text-sm">Your saved task templates</p>
        <Separator className="mb-8 mt-2" />
        <SavedTasks />
      </section>
      <section className="py-4">
        <header>
          <h1 className="text-3xl">Skyvern Templates</h1>
        </header>
        <p className="mt-1 text-sm">
          Sample tasks that showcase Skyvern's capabilities
        </p>
        <Separator className="mb-8 mt-2" />
        <div className="grid grid-cols-4 gap-4">
          {Object.entries(templateSamples).map(([sampleKey, sample]) => {
            return (
              <Card key={sampleKey}>
                <CardHeader>
                  <CardTitle>{sample.title}</CardTitle>
                  <CardDescription className="overflow-hidden text-ellipsis whitespace-nowrap">
                    {getSample(sampleKey as SampleCase).url}
                  </CardDescription>
                </CardHeader>
                <CardContent
                  className="h-48 cursor-pointer hover:bg-muted/40"
                  onClick={() => {
                    navigate(sampleKey);
                  }}
                >
                  {sample.description}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </section>
    </div>
  );
}

export { TaskTemplates };
