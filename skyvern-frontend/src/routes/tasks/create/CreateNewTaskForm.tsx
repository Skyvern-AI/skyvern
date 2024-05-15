import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  dataExtractionGoalDescription,
  extractedInformationSchemaDescription,
  navigationGoalDescription,
  navigationPayloadDescription,
  urlDescription,
  webhookCallbackUrlDescription,
} from "../data/descriptionHelperContent";
import { Textarea } from "@/components/ui/textarea";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useToast } from "@/components/ui/use-toast";
import { InfoCircledIcon } from "@radix-ui/react-icons";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { ToastAction } from "@radix-ui/react-toast";
import { Link } from "react-router-dom";
import fetchToCurl from "fetch-to-curl";
import { apiBaseUrl } from "@/util/env";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useApiCredential } from "@/hooks/useApiCredential";

const createNewTaskFormSchema = z.object({
  url: z.string().url({
    message: "Invalid URL",
  }),
  webhookCallbackUrl: z.string().optional(), // url maybe, but shouldn't be validated as one
  navigationGoal: z.string().optional(),
  dataExtractionGoal: z.string().optional(),
  navigationPayload: z.string().optional(),
  extractedInformationSchema: z.string().optional(),
});

export type CreateNewTaskFormValues = z.infer<typeof createNewTaskFormSchema>;

type Props = {
  initialValues: CreateNewTaskFormValues;
};

function createTaskRequestObject(formValues: CreateNewTaskFormValues) {
  return {
    url: formValues.url,
    webhook_callback_url: formValues.webhookCallbackUrl ?? "",
    navigation_goal: formValues.navigationGoal ?? "",
    data_extraction_goal: formValues.dataExtractionGoal ?? "",
    proxy_location: "NONE",
    error_code_mapping: null,
    navigation_payload: formValues.navigationPayload ?? "",
    extracted_information_schema: formValues.extractedInformationSchema ?? "",
  };
}

function CreateNewTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();

  const form = useForm<CreateNewTaskFormValues>({
    resolver: zodResolver(createNewTaskFormSchema),
    defaultValues: initialValues,
  });

  const mutation = useMutation({
    mutationFn: async (formValues: CreateNewTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      const client = await getClient(credentialGetter);
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
    onSuccess: (response) => {
      toast({
        title: "Task Created",
        description: `${response.data.task_id} created successfully.`,
        action: (
          <ToastAction altText="View">
            <Button asChild>
              <Link to={`/tasks/${response.data.task_id}`}>View</Link>
            </Button>
          </ToastAction>
        ),
      });
      queryClient.invalidateQueries({
        queryKey: ["tasks"],
      });
    },
  });

  function onSubmit(values: CreateNewTaskFormValues) {
    mutation.mutate(values);
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-8">
        <FormField
          control={form.control}
          name="url"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  URL*
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{urlDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Input placeholder="example.com" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="webhookCallbackUrl"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  Webhook Callback URL
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{webhookCallbackUrlDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Input placeholder="example.com" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="navigationGoal"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  Navigation Goal
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{navigationGoalDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Textarea rows={5} placeholder="Navigation Goal" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="dataExtractionGoal"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  Data Extraction Goal
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{dataExtractionGoalDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Data Extraction Goal"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="navigationPayload"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  Navigation Payload
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{navigationPayloadDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Navigation Payload"
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="extractedInformationSchema"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  Extracted Information Schema
                  <TooltipProvider>
                    <Tooltip>
                      <TooltipTrigger asChild>
                        <InfoCircledIcon />
                      </TooltipTrigger>
                      <TooltipContent className="max-w-[250px]">
                        <p>{extractedInformationSchemaDescription}</p>
                      </TooltipContent>
                    </Tooltip>
                  </TooltipProvider>
                </div>
              </FormLabel>
              <FormControl>
                <Textarea
                  placeholder="Extracted Information Schema"
                  rows={5}
                  {...field}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="outline"
            onClick={async () => {
              const curl = fetchToCurl({
                method: "POST",
                url: `${apiBaseUrl}/tasks`,
                body: createTaskRequestObject(form.getValues()),
                headers: {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                },
              });
              await navigator.clipboard.writeText(curl);
              toast({
                title: "Copied cURL",
                description: "cURL copied to clipboard",
              });
            }}
          >
            Copy cURL
          </Button>
          <Button type="submit">Create</Button>
        </div>
      </form>
    </Form>
  );
}

export { CreateNewTaskForm };
