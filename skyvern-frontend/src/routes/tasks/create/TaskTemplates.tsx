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
import { useMutation } from "@tanstack/react-query";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { AxiosError } from "axios";
import { toast } from "@/components/ui/use-toast";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";

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

function TaskTemplates() {
  const navigate = useNavigate();
  const [prompt, setPrompt] = useState<string>("");
  const credentialGetter = useCredentialGetter();

  const getTaskFromPromptMutation = useMutation({
    mutationFn: async (prompt: string) => {
      const client = await getClient(credentialGetter);
      return client
        .post("/generate/task", { prompt })
        .then((response) => response.data);
    },
    onSuccess: (response) => {
      navigate("/create/sk-prompt", { state: { data: response } });
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
            className="underline underline-offset-2 ml-auto"
          >
            Book a demo {"->"}
          </a>
        </AlertDescription>
      </Alert>
      <section className="py-4">
        <header>
          <h1 className="text-3xl mb-2">Try a prompt</h1>
        </header>
        <p className="text-sm">
          We will generate a task for you automatically.
        </p>
        <Separator className="mt-2 mb-8" />
        <div className="flex border rounded-xl items-center pr-4 max-w-xl mx-auto">
          <Textarea
            className="rounded-xl resize-none border-transparent hover:border-transparent focus-visible:ring-0 p-2 font-mono text-sm"
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Enter your prompt..."
          />
          <div className="h-full">
            {getTaskFromPromptMutation.isPending ? (
              <ReloadIcon className="w-6 h-6 animate-spin" />
            ) : (
              <PaperPlaneIcon
                className="w-6 h-6 cursor-pointer"
                onClick={() => {
                  getTaskFromPromptMutation.mutate(prompt);
                }}
              />
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-4 mt-4 justify-center">
          {examplePrompts.map((examplePrompt) => {
            return (
              <div
                key={examplePrompt}
                className="p-2 border rounded-xl cursor-pointer text-muted-foreground text-sm"
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
        <p className="text-sm mt-1">Your saved task templates</p>
        <Separator className="mt-2 mb-8" />
        <SavedTasks />
      </section>
      <section className="py-4">
        <header>
          <h1 className="text-3xl">Skyvern Templates</h1>
        </header>
        <p className="text-sm mt-1">
          Sample tasks that showcase Skyvern's capabilities
        </p>
        <Separator className="mt-2 mb-8" />
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
                  className="h-48 hover:bg-muted/40 cursor-pointer"
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
