import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useToast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiBaseUrl } from "@/util/env";
import { zodResolver } from "@hookform/resolvers/zod";
import { InfoCircledIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import fetchToCurl from "fetch-to-curl";
import { useForm, useFormState } from "react-hook-form";
import { Link, useParams } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { z } from "zod";
import {
  dataExtractionGoalDescription,
  extractedInformationSchemaDescription,
  navigationGoalDescription,
  navigationPayloadDescription,
  urlDescription,
  webhookCallbackUrlDescription,
} from "../data/descriptionHelperContent";
import { SubmitEvent } from "@/types";
import { AxiosError } from "axios";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { OrganizationApiResponse } from "@/api/types";
import { MAX_STEPS_DEFAULT } from "../constants";
import { Skeleton } from "@/components/ui/skeleton";

const savedTaskFormSchema = z
  .object({
    title: z.string().min(1, "Title is required"),
    description: z.string(),
    url: z.string().url({
      message: "Invalid URL",
    }),
    proxyLocation: z.string().or(z.null()).optional(),
    webhookCallbackUrl: z.string().or(z.null()).optional(), // url maybe, but shouldn't be validated as one
    navigationGoal: z.string().or(z.null()).optional(),
    dataExtractionGoal: z.string().or(z.null()).optional(),
    navigationPayload: z.string().or(z.null()).optional(),
    extractedInformationSchema: z.string().or(z.null()).optional(),
    maxSteps: z.number().optional(),
  })
  .superRefine(
    (
      { navigationGoal, dataExtractionGoal, extractedInformationSchema },
      ctx,
    ) => {
      if (!navigationGoal && !dataExtractionGoal) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "At least one of navigation goal or data extraction goal must be provided",
          path: ["navigationGoal"],
        });
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message:
            "At least one of navigation goal or data extraction goal must be provided",
          path: ["dataExtractionGoal"],
        });
        return z.NEVER;
      }
      if (extractedInformationSchema) {
        try {
          JSON.parse(extractedInformationSchema);
        } catch (e) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: "Invalid JSON",
            path: ["extractedInformationSchema"],
          });
        }
      }
    },
  );

export type SavedTaskFormValues = z.infer<typeof savedTaskFormSchema>;

type Props = {
  initialValues: SavedTaskFormValues;
};

function transform(value: unknown) {
  return value === "" ? null : value;
}

function createTaskRequestObject(formValues: SavedTaskFormValues) {
  let extractedInformationSchema = null;
  if (formValues.extractedInformationSchema) {
    try {
      extractedInformationSchema = JSON.parse(
        formValues.extractedInformationSchema,
      );
    } catch (e) {
      extractedInformationSchema = formValues.extractedInformationSchema;
    }
  }

  return {
    url: formValues.url,
    webhook_callback_url: transform(formValues.webhookCallbackUrl),
    navigation_goal: transform(formValues.navigationGoal),
    data_extraction_goal: transform(formValues.dataExtractionGoal),
    proxy_location: transform(formValues.proxyLocation),
    error_code_mapping: null,
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: extractedInformationSchema,
  };
}

function createTaskTemplateRequestObject(values: SavedTaskFormValues) {
  let extractedInformationSchema = null;
  if (values.extractedInformationSchema) {
    try {
      extractedInformationSchema = JSON.parse(
        values.extractedInformationSchema,
      );
    } catch (e) {
      extractedInformationSchema = values.extractedInformationSchema;
    }
  }

  return {
    title: values.title,
    description: values.description,
    is_saved_task: true,
    webhook_callback_url: values.webhookCallbackUrl,
    proxy_location: values.proxyLocation,
    workflow_definition: {
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "json",
          key: "navigation_payload",
          default_value: JSON.stringify(values.navigationPayload),
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: "Task 1",
          url: values.url,
          navigation_goal: values.navigationGoal,
          data_extraction_goal: values.dataExtractionGoal,
          data_schema: extractedInformationSchema,
          max_steps_per_run: values.maxSteps,
        },
      ],
    },
  };
}

function SavedTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const { template } = useParams();

  const { data: organizations, isPending: organizationIsPending } = useQuery<
    Array<OrganizationApiResponse>
  >({
    queryKey: ["organizations"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get("/organizations")
        .then((response) => response.data.organizations);
    },
  });

  const organization = organizations?.[0];

  const form = useForm<SavedTaskFormValues>({
    resolver: zodResolver(savedTaskFormSchema),
    defaultValues: initialValues,
    values: {
      ...initialValues,
      maxSteps:
        initialValues.maxSteps ??
        organization?.max_steps_per_run ??
        MAX_STEPS_DEFAULT,
    },
  });

  const { isDirty } = useFormState({ control: form.control });

  const createTaskMutation = useMutation({
    mutationFn: async (formValues: SavedTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      const client = await getClient(credentialGetter);
      const includeOverrideHeader =
        formValues.maxSteps !== organization?.max_steps_per_run &&
        formValues.maxSteps !== MAX_STEPS_DEFAULT;
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest, {
        ...(includeOverrideHeader && {
          headers: {
            "x-max-steps-override": formValues.maxSteps ?? MAX_STEPS_DEFAULT,
          },
        }),
      });
    },
    onError: (error: AxiosError) => {
      if (error.response?.status === 402) {
        toast({
          variant: "destructive",
          title: "Failed to create task",
          description:
            "You don't have enough credits to run this task. Go to billing to see your credit balance.",
          action: (
            <ToastAction altText="Go to Billing">
              <Button asChild>
                <Link to="billing">Go to Billing</Link>
              </Button>
            </ToastAction>
          ),
        });
        return;
      }
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

  const saveTaskMutation = useMutation({
    mutationFn: async (formValues: SavedTaskFormValues) => {
      const saveTaskRequest = createTaskTemplateRequestObject(formValues);
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(saveTaskRequest);
      return client
        .put(`/workflows/${template}`, yaml, {
          headers: {
            "Content-Type": "text/plain",
          },
        })
        .then((response) => response.data);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "There was an error while saving changes",
        description: error.message,
      });
    },
    onSuccess: () => {
      toast({
        title: "Changes saved",
        description: "Changes saved successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows", template],
      });
    },
  });

  function handleCreate(values: SavedTaskFormValues) {
    createTaskMutation.mutate(values);
  }

  function handleSave(values: SavedTaskFormValues) {
    saveTaskMutation.mutate(values);
  }

  return (
    <Form {...form}>
      <form
        onSubmit={(event) => {
          const submitter = (
            (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement
          ).value;
          if (submitter === "save") {
            form.handleSubmit(handleSave)(event);
          }
          if (submitter === "create") {
            form.handleSubmit(handleCreate)(event);
          }
        }}
        className="space-y-8"
      >
        <FormField
          control={form.control}
          name="title"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Title *</FormLabel>
              <FormControl>
                <Input placeholder="Title" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="description"
          render={({ field }) => (
            <FormItem>
              <FormLabel>Description</FormLabel>
              <FormControl>
                <Textarea placeholder="Description" {...field} />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <FormField
          control={form.control}
          name="url"
          render={({ field }) => (
            <FormItem>
              <FormLabel>
                <div className="flex gap-2">
                  URL *
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
              <FormDescription>The starting URL for the task</FormDescription>
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
              <FormDescription>
                How do you want Skyvern to navigate?
              </FormDescription>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Navigation Goal"
                  {...field}
                  value={field.value === null ? "" : field.value}
                />
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
              <FormDescription>
                If you want Skyvern to extract data after it's finished
                navigating
              </FormDescription>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Data Extraction Goal"
                  {...field}
                  value={field.value === null ? "" : field.value}
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
              <FormDescription>
                Any context Skyvern needs to complete its actions (ex. text that
                may be required to fill out forms)
              </FormDescription>
              <FormControl>
                <Textarea
                  rows={5}
                  placeholder="Navigation Payload"
                  {...field}
                  value={field.value === null ? "" : field.value}
                />
              </FormControl>
              <FormMessage />
            </FormItem>
          )}
        />
        <Accordion type="single" collapsible>
          <AccordionItem value="advanced-settings">
            <AccordionTrigger>Advanced Settings</AccordionTrigger>
            <AccordionContent className="space-y-8 px-1 py-4">
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
                    <FormDescription>
                      Jsonc schema to force the json format for extracted
                      information
                    </FormDescription>
                    <FormControl>
                      <Textarea
                        placeholder="Extracted Information Schema"
                        rows={5}
                        {...field}
                        value={field.value === null ? "" : field.value}
                      />
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
                    <FormDescription>
                      The URL of a webhook endpoint to send the extracted
                      information
                    </FormDescription>
                    <FormControl>
                      <Input
                        placeholder="example.com"
                        {...field}
                        value={field.value === null ? "" : field.value}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="maxSteps"
                render={({ field }) => {
                  return (
                    <FormItem>
                      <FormLabel>Max Steps</FormLabel>
                      <FormDescription>
                        Max steps for this task. This will override your
                        organization wide setting.
                      </FormDescription>
                      <FormControl>
                        {organizationIsPending ? (
                          <Skeleton className="h-8" />
                        ) : (
                          <Input
                            {...field}
                            type="number"
                            min={1}
                            max={
                              organization?.max_steps_per_run ??
                              MAX_STEPS_DEFAULT
                            }
                            value={field.value ?? MAX_STEPS_DEFAULT}
                            onChange={(event) => {
                              field.onChange(parseInt(event.target.value));
                            }}
                          />
                        )}
                      </FormControl>
                    </FormItem>
                  );
                }}
              />
            </AccordionContent>
          </AccordionItem>
        </Accordion>

        <div className="flex justify-end gap-3">
          <Button
            type="button"
            variant="secondary"
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
          <Button
            type="submit"
            name="save"
            value="save"
            variant="secondary"
            disabled={saveTaskMutation.isPending || !isDirty}
          >
            {saveTaskMutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            Save Changes
          </Button>
          <Button
            type="submit"
            name="create"
            value="create"
            disabled={createTaskMutation.isPending}
          >
            {createTaskMutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            Run Task
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { SavedTaskForm };
