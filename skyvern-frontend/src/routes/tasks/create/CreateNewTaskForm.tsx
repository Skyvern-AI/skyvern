import { getClient } from "@/api/AxiosClient";
import {
  CreateTaskRequest,
  OrganizationApiResponse,
  ProxyLocation,
  RunEngine,
} from "@/api/types";
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
import { toast } from "@/components/ui/use-toast";
import { KeyValueInput } from "@/components/KeyValueInput";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { runsApiBaseUrl } from "@/util/env";
import { CopyApiCommandDropdown } from "@/components/CopyApiCommandDropdown";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { buildTaskRunPayload } from "@/util/taskRunPayload";
import { zodResolver } from "@hookform/resolvers/zod";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";
import { useForm, useFormState } from "react-hook-form";
import { Link } from "react-router-dom";
import { MAX_STEPS_DEFAULT } from "../constants";
import { TaskFormSection } from "./TaskFormSection";
import {
  createNewTaskFormSchema,
  CreateNewTaskFormValues,
} from "./taskFormTypes";
import { ProxySelector } from "@/components/ProxySelector";
import { Switch } from "@/components/ui/switch";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "@/routes/workflows/editor/nodes/Taskv2Node/types";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";
type Props = {
  initialValues: CreateNewTaskFormValues;
};

function transform<T>(value: T): T | null {
  return value === "" ? null : value;
}

function createTaskRequestObject(
  formValues: CreateNewTaskFormValues,
): CreateTaskRequest {
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
  let extraHttpHeaders = null;
  if (formValues.extraHttpHeaders) {
    try {
      extraHttpHeaders = JSON.parse(formValues.extraHttpHeaders);
    } catch (e) {
      extraHttpHeaders = formValues.extraHttpHeaders;
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
    title: null,
    url: formValues.url,
    webhook_callback_url: transform(formValues.webhookCallbackUrl),
    navigation_goal: transform(formValues.navigationGoal),
    data_extraction_goal: transform(formValues.dataExtractionGoal),
    proxy_location: formValues.proxyLocation ?? ProxyLocation.Residential,
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: extractedInformationSchema,
    extra_http_headers: extraHttpHeaders,
    totp_identifier: transform(formValues.totpIdentifier),
    browser_address: transform(formValues.cdpAddress),
    error_code_mapping: errorCodeMapping,
    max_screenshot_scrolls: formValues.maxScreenshotScrolls,
    include_action_history_in_verification:
      formValues.includeActionHistoryInVerification,
  };
}

type Section = "base" | "extraction" | "advanced";

function CreateNewTaskForm({ initialValues }: Props) {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const apiCredential = useApiCredential();
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

  const form = useForm<CreateNewTaskFormValues>({
    resolver: zodResolver(createNewTaskFormSchema),
    defaultValues: {
      ...initialValues,
      maxStepsOverride: initialValues.maxStepsOverride ?? null,
      proxyLocation: initialValues.proxyLocation ?? ProxyLocation.Residential,
      maxScreenshotScrolls: initialValues.maxScreenshotScrolls ?? null,
      cdpAddress: initialValues.cdpAddress ?? null,
    },
  });
  const { errors } = useFormState({ control: form.control });

  const mutation = useMutation({
    mutationFn: async (formValues: CreateNewTaskFormValues) => {
      const taskRequest = createTaskRequestObject(formValues);
      const client = await getClient(credentialGetter);
      const includeOverrideHeader =
        formValues.maxStepsOverride !== null &&
        formValues.maxStepsOverride !== MAX_STEPS_DEFAULT;
      return client.post<
        ReturnType<typeof createTaskRequestObject>,
        { data: { task_id: string } }
      >("/tasks", taskRequest, {
        ...(includeOverrideHeader && {
          headers: {
            "x-max-steps-override": formValues.maxStepsOverride,
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
      queryClient.invalidateQueries({
        queryKey: ["runs"],
      });
    },
  });

  function onSubmit(values: CreateNewTaskFormValues) {
    mutation.mutate(values);
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
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
        <TaskFormSection
          index={1}
          title="Base Content"
          active={isActive("base")}
          onClick={() => {
            toggleSection("base");
          }}
          hasError={
            typeof errors.url !== "undefined" ||
            typeof errors.navigationGoal !== "undefined"
          }
        >
          {isActive("base") && (
            <div className="space-y-6">
              <div className="space-y-4">
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
            typeof errors.dataExtractionGoal !== "undefined" ||
            typeof errors.extractedInformationSchema !== "undefined"
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
                  name="includeActionHistoryInVerification"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Include Action History</h1>
                            <h2 className="text-base text-slate-400">
                              Whether to include action history when verifying
                              the task completion.
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Switch
                              checked={field.value ?? false}
                              onCheckedChange={(checked) => {
                                field.onChange(checked);
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
                            <div className="flex flex-col gap-2">
                              <Input
                                className="w-full"
                                {...field}
                                placeholder="https://"
                                value={field.value === null ? "" : field.value}
                              />
                              <TestWebhookDialog
                                runType="task"
                                runId={null}
                                initialWebhookUrl={
                                  field.value === null ? undefined : field.value
                                }
                                trigger={
                                  <Button
                                    type="button"
                                    variant="secondary"
                                    className="self-start"
                                    disabled={!field.value}
                                  >
                                    Test Webhook
                                  </Button>
                                }
                              />
                            </div>
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
                                className="w-48"
                              />
                            </FormControl>
                            <FormMessage />
                          </div>
                        </div>
                      </FormItem>
                    );
                  }}
                />
                <FormField
                  control={form.control}
                  name="maxScreenshotScrolls"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Max Screenshot Scrolls</h1>
                            <h2 className="text-base text-slate-400">
                              {`The maximum number of scrolls for the post action screenshot. Default is ${MAX_SCREENSHOT_SCROLLS_DEFAULT}. If it's set to 0, it will take the current viewport screenshot.`}
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              type="number"
                              min={0}
                              value={field.value ?? ""}
                              placeholder={`Default: ${MAX_SCREENSHOT_SCROLLS_DEFAULT}`}
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
                <Separator />
                <FormField
                  control={form.control}
                  name="extraHttpHeaders"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Extra HTTP Headers</h1>
                            <h2 className="text-base text-slate-400">
                              Specify some self defined HTTP requests headers in
                              Dict format
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <KeyValueInput
                              value={field.value ?? ""}
                              onChange={(val) => field.onChange(val)}
                              addButtonText="Add Header"
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
                <FormField
                  control={form.control}
                  name="cdpAddress"
                  render={({ field }) => (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel>
                          <div className="w-72">
                            <h1 className="text-lg">Browser Address</h1>
                            <h2 className="text-base text-slate-400">
                              The address of the Browser server to use for the
                              task run.
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full">
                          <FormControl>
                            <Input
                              {...field}
                              placeholder="http://127.0.0.1:9222"
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
          <CopyApiCommandDropdown
            getOptions={() => {
              const formValues = form.getValues();
              const includeOverrideHeader =
                formValues.maxStepsOverride !== null &&
                formValues.maxStepsOverride !== MAX_STEPS_DEFAULT;

              const headers: Record<string, string> = {
                "Content-Type": "application/json",
                "x-api-key": apiCredential ?? "<your-api-key>",
              };

              if (includeOverrideHeader) {
                headers["x-max-steps-override"] = String(
                  formValues.maxStepsOverride,
                );
              }

              return {
                method: "POST",
                url: `${runsApiBaseUrl}/run/tasks`,
                body: buildTaskRunPayload(
                  createTaskRequestObject(formValues),
                  RunEngine.SkyvernV1,
                ),
                headers,
              } satisfies ApiCommandOptions;
            }}
          />
          <Button type="submit" disabled={mutation.isPending}>
            {mutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            <PlayIcon className="mr-2 h-4 w-4" />
            Run
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { CreateNewTaskForm };
