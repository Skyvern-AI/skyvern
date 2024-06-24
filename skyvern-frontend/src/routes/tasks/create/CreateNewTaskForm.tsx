import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
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
import {
  dataExtractionGoalDescription,
  extractedInformationSchemaDescription,
  navigationGoalDescription,
  navigationPayloadDescription,
  urlDescription,
  webhookCallbackUrlDescription,
} from "../data/descriptionHelperContent";
import { Textarea } from "@/components/ui/textarea";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useToast } from "@/components/ui/use-toast";
import { InfoCircledIcon, ReloadIcon } from "@radix-ui/react-icons";
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
import { AxiosError } from "axios";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { OrganizationApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { MAX_STEPS_DEFAULT } from "../constants";

const createNewTaskFormSchema = z
  .object({
    url: z.string().url({
      message: "Invalid URL",
    }),
    webhookCallbackUrl: z.string().or(z.null()).optional(), // url maybe, but shouldn't be validated as one
    navigationGoal: z.string().or(z.null()).optional(),
    dataExtractionGoal: z.string().or(z.null()).optional(),
    navigationPayload: z.string().or(z.null()).optional(),
    extractedInformationSchema: z.string().or(z.null()).optional(),
    maxStepsOverride: z.number().optional(),
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

export type CreateNewTaskFormValues = z.infer<typeof createNewTaskFormSchema>;

type Props = {
  initialValues: CreateNewTaskFormValues;
};

function transform(value: unknown) {
  return value === "" ? null : value;
}

function createTaskRequestObject(formValues: CreateNewTaskFormValues) {
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
    proxy_location: "RESIDENTIAL",
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: extractedInformationSchema,
  };
}

function CreateNewTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();

  const { data: organizations, isPending } = useQuery<
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

  const form = useForm<CreateNewTaskFormValues>({
    resolver: zodResolver(createNewTaskFormSchema),
    defaultValues: initialValues,
    values: {
      ...initialValues,
      maxStepsOverride: organization?.max_steps_per_run ?? MAX_STEPS_DEFAULT,
    },
  });

  const mutation = useMutation({
    mutationFn: async (formValues: CreateNewTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      const client = await getClient(credentialGetter);
      const includeOverrideHeader =
        formValues.maxStepsOverride !== organization?.max_steps_per_run &&
        formValues.maxStepsOverride !== MAX_STEPS_DEFAULT;
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest, {
        ...(includeOverrideHeader && {
          headers: {
            "x-max-steps-override":
              formValues.maxStepsOverride ?? MAX_STEPS_DEFAULT,
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
        title: "There was an error creating the task.",
        description: error.message,
      });
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
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
                name="maxStepsOverride"
                render={({ field }) => {
                  return (
                    <FormItem>
                      <FormLabel>Max Steps</FormLabel>
                      <FormDescription>
                        Max steps for this task. This will override your
                        organization wide setting.
                      </FormDescription>
                      <FormControl>
                        {isPending ? (
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
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            Create
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { CreateNewTaskForm };
