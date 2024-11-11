import { getClient } from "@/api/AxiosClient";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
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
import { Separator } from "@/components/ui/separator";
import { useToast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { SubmitEvent } from "@/types";
import { copyText } from "@/util/copyText";
import { apiBaseUrl } from "@/util/env";
import { zodResolver } from "@hookform/resolvers/zod";
import { CopyIcon, PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import fetchToCurl from "fetch-to-curl";
import { useState } from "react";
import { useForm, useFormState } from "react-hook-form";
import { Link, useParams } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { MAX_STEPS_DEFAULT } from "../constants";
import { TaskFormSection } from "./TaskFormSection";
import { savedTaskFormSchema, SavedTaskFormValues } from "./taskFormTypes";
import { OrganizationApiResponse, ProxyLocation } from "@/api/types";
import { ProxySelector } from "@/components/ProxySelector";

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

  let errorCodeMapping = null;
  if (formValues.errorCodeMapping) {
    try {
      errorCodeMapping = JSON.parse(formValues.errorCodeMapping);
    } catch (e) {
      errorCodeMapping = formValues.errorCodeMapping;
    }
  }

  return {
    url: formValues.url,
    webhook_callback_url: transform(formValues.webhookCallbackUrl),
    navigation_goal: transform(formValues.navigationGoal),
    data_extraction_goal: transform(formValues.dataExtractionGoal),
    proxy_location: transform(formValues.proxyLocation),
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: extractedInformationSchema,
    totp_verification_url: transform(formValues.totpVerificationUrl),
    totp_identifier: transform(formValues.totpIdentifier),
    error_code_mapping: errorCodeMapping,
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

  let errorCodeMapping = null;
  if (values.errorCodeMapping) {
    try {
      errorCodeMapping = JSON.parse(values.errorCodeMapping);
    } catch (e) {
      errorCodeMapping = values.errorCodeMapping;
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
          max_steps_per_run: values.maxStepsOverride,
          totp_verification_url: values.totpVerificationUrl,
          totp_identifier: values.totpIdentifier,
          error_code_mapping: errorCodeMapping,
        },
      ],
    },
  };
}

type Section = "base" | "extraction" | "advanced";

function SavedTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
  const { template } = useParams();
  const [activeSections, setActiveSections] = useState<Array<Section>>([
    "base",
  ]);
  const [showAdvancedBaseContent, setShowAdvancedBaseContent] = useState(false);

  const { data: organizations } = useQuery<Array<OrganizationApiResponse>>({
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
    defaultValues: {
      ...initialValues,
      maxStepsOverride: initialValues.maxStepsOverride ?? null,
      proxyLocation: initialValues.proxyLocation ?? ProxyLocation.Residential,
    },
  });

  const { isDirty, errors } = useFormState({ control: form.control });

  const createAndSaveTaskMutation = useMutation({
    mutationFn: async (formValues: SavedTaskFormValues) => {
      const saveTaskRequest = createTaskTemplateRequestObject(formValues);
      const yaml = convertToYAML(saveTaskRequest);
      const client = await getClient(credentialGetter);

      return client
        .put(`/workflows/${template}`, yaml, {
          headers: {
            "Content-Type": "text/plain",
          },
        })
        .then(() => {
          const taskRequest = createTaskRequestObject(formValues);
          const includeOverrideHeader =
            formValues.maxStepsOverride !== null &&
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
      queryClient.invalidateQueries({
        queryKey: ["savedTasks"],
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
        variant: "success",
        title: "Changes saved",
        description: "Changes saved successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["savedTasks", template],
      });
    },
  });

  function handleCreate(values: SavedTaskFormValues) {
    createAndSaveTaskMutation.mutate(values);
  }

  function handleSave(values: SavedTaskFormValues) {
    saveTaskMutation.mutate(values);
  }

  function isActive(section: Section) {
    return activeSections.includes(section);
  }

  function toggleSection(section: Section) {
    if (isActive(section)) {
      setActiveSections(activeSections.filter((s) => s !== section));
    } else {
      setActiveSections([...activeSections, section]);
    }
  }

  return (
    <Form {...form}>
      <form
        onSubmit={(event) => {
          const submitter = (
            (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement
          ).value;
          if (submitter === "create") {
            form.handleSubmit(handleCreate)(event);
          }
          if (submitter === "save") {
            form.handleSubmit(handleSave)(event);
          }
        }}
        className="space-y-4"
      >
        <TaskFormSection
          index={1}
          title="Base Content"
          active={isActive("base")}
          onClick={() => {
            toggleSection("base");
          }}
          hasError={
            typeof errors.navigationGoal !== "undefined" ||
            typeof errors.title !== "undefined" ||
            typeof errors.url !== "undefined" ||
            typeof errors.description !== "undefined"
          }
        >
          {isActive("base") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="title"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Title</h1>
                            <h2 className="text-base text-slate-400">
                              Name of your task
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input placeholder="Task Name" {...field} />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="description"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Description</h1>
                            <h2 className="text-base text-slate-400">
                              What is the purpose of the task?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <AutoResizingTextarea
                              placeholder="This template is used to..."
                              {...field}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <Separator />
                <FormField
                  control={form.control}
                  name="url"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">URL</h1>
                            <h2 className="text-base text-slate-400">
                              The starting URL for the task
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input placeholder="https://" {...field} />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="navigationGoal"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Navigation Goal</h1>
                            <h2 className="text-base text-slate-400">
                              Where should Skyvern go and what should Skyvern
                              do?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <AutoResizingTextarea
                              {...field}
                              placeholder="Tell Skyvern what to do."
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                {showAdvancedBaseContent ? (
                  <div className="border-t border-dashed pt-4">
                    <FormField
                      control={form.control}
                      name="navigationPayload"
                      render={({ field }) => (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <h1 className="text-lg">Navigation Payload</h1>
                                <h2 className="text-base text-slate-400">
                                  Specify important parameters, routes, or
                                  states
                                </h2>
                              </div>
                              <Button
                                className="mt-4"
                                type="button"
                                variant="tertiary"
                                onClick={() => {
                                  setShowAdvancedBaseContent(false);
                                }}
                                size="sm"
                              >
                                Hide Advanced Settings
                              </Button>
                            </FormLabel>
                            <div className="w-full">
                              <FormControl>
                                <CodeEditor
                                  {...field}
                                  language="json"
                                  minHeight="96px"
                                  maxHeight="500px"
                                  value={
                                    field.value === null ? "" : field.value
                                  }
                                />
                              </FormControl>
                              <FormMessage />
                            </div>
                          </div>
                        </FormItem>
                      )}
                    />
                  </div>
                ) : (
                  <div>
                    <Button
                      type="button"
                      variant="tertiary"
                      onClick={() => {
                        setShowAdvancedBaseContent(true);
                      }}
                      size="sm"
                    >
                      Show Advanced Settings
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </TaskFormSection>
        <TaskFormSection
          index={2}
          title="Extraction"
          active={isActive("extraction")}
          onClick={() => {
            toggleSection("extraction");
          }}
          hasError={
            typeof errors.extractedInformationSchema !== "undefined" ||
            typeof errors.dataExtractionGoal !== "undefined"
          }
        >
          {isActive("extraction") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="dataExtractionGoal"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Data Extraction Goal</h1>
                            <h2 className="text-base text-slate-400">
                              What outputs are you looking to get?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <AutoResizingTextarea
                              {...field}
                              placeholder="What data do you need to extract?"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="extractedInformationSchema"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Data Schema</h1>
                            <h2 className="text-base text-slate-400">
                              Specify the output format in JSON
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <CodeEditor
                              {...field}
                              language="json"
                              minHeight="96px"
                              maxHeight="500px"
                              value={
                                field.value === null ||
                                typeof field.value === "undefined"
                                  ? ""
                                  : field.value
                              }
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
              </div>
            </div>
          )}
        </TaskFormSection>
        <TaskFormSection
          index={3}
          title="Advanced Settings"
          active={isActive("advanced")}
          onClick={() => {
            toggleSection("advanced");
          }}
          hasError={
            typeof errors.navigationPayload !== "undefined" ||
            typeof errors.maxStepsOverride !== "undefined" ||
            typeof errors.webhookCallbackUrl !== "undefined" ||
            typeof errors.errorCodeMapping !== "undefined"
          }
        >
          {isActive("advanced") && (
            <div className="space-y-6">
              <div className="space-y-4">
                <FormField
                  control={form.control}
                  name="maxStepsOverride"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Max Steps Override</h1>
                            <h2 className="text-base text-slate-400">
                              Want to allow this task to execute more or less
                              steps than the default?
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              type="number"
                              min={1}
                              value={field.value ?? ""}
                              placeholder={`Default: ${organization?.max_steps_per_run ?? MAX_STEPS_DEFAULT}`}
                              onChange={(event) => {
                                const value =
                                  event.target.value === ""
                                    ? null
                                    : Number(event.target.value);
                                field.onChange(value);
                              }}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="webhookCallbackUrl"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Webhook Callback URL</h1>
                            <h2 className="text-base text-slate-400">
                              The URL of a webhook endpoint to send the
                              extracted information
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="https://"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="proxyLocation"
                  render={({ field }) => {
                    return (
                      <FormItem>
                        <div className="flex gap-16">
                          <FormLabel>
                            <div className="w-72">
                              <div className="flex items-center gap-2 text-lg">
                                Proxy Location
                              </div>
                              <h2 className="text-sm text-slate-400">
                                Route Skyvern through one of our available
                                proxies.
                              </h2>
                            </div>
                          </FormLabel>
                          <div className="w-full space-y-2">
                            <FormControl>
                              <ProxySelector
                                value={field.value}
                                onChange={field.onChange}
                              />
                            </FormControl>
                            <FormMessage />
                          </div>
                        </div>
                      </FormItem>
                    );
                  }}
                />
                <Separator />
                <FormField
                  control={form.control}
                  name="errorCodeMapping"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Error Messages</h1>
                            <h2 className="text-base text-slate-400">
                              Specify any error outputs you would like to be
                              notified about
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <CodeEditor
                              {...field}
                              language="json"
                              minHeight="96px"
                              maxHeight="500px"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <Separator />
                <FormField
                  control={form.control}
                  name="totpVerificationUrl"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">2FA Verification URL</h1>
                            <h2 className="text-base text-slate-400"></h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="Provide your 2FA endpoint"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="totpIdentifier"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">2FA Identifier</h1>
                            <h2 className="text-base text-slate-400"></h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="Add an ID that links your TOTP to the task"
                              value={field.value === null ? "" : field.value}
                            />
                          </FormControl>
                          <FormMessage />
                        </div>
                      </div>
                    </FormItem>
                  )}
                />
              </div>
            </div>
          )}
        </TaskFormSection>

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
              copyText(curl).then(() => {
                toast({
                  variant: "success",
                  title: "Copied successfully",
                  description: "cURL copied to clipboard",
                });
              });
            }}
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            cURL
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
            disabled={createAndSaveTaskMutation.isPending}
          >
            {createAndSaveTaskMutation.isPending ? (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <PlayIcon className="mr-2 h-4 w-4" />
            )}
            Run
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { SavedTaskForm };
