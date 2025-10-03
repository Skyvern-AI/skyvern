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
import { apiBaseUrl } from "@/util/env";
import { CopyApiCommandDropdown } from "@/components/CopyApiCommandDropdown";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { zodResolver } from "@hookform/resolvers/zod";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AxiosError } from "axios";
import { useState } from "react";
import { useForm, useFormState } from "react-hook-form";
import { Link, useParams } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { MAX_STEPS_DEFAULT } from "../constants";
import { TaskFormSection } from "./TaskFormSection";
import { savedTaskFormSchema, SavedTaskFormValues } from "./taskFormTypes";
import { OrganizationApiResponse, ProxyLocation } from "@/api/types";
import { ProxySelector } from "@/components/ProxySelector";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogClose,
} from "@/components/ui/dialog";
import { AnimatedWave } from "@/components/AnimatedWave";

type Props = {
  initialValues: SavedTaskFormValues;
};

function transform(value: unknown) {
  return value === "" ? null : value;
}

function createTaskRequestObject(formValues: SavedTaskFormValues) {
  return {
    title: formValues.title,
    url: formValues.url,
    webhook_callback_url: transform(formValues.webhookCallbackUrl),
    navigation_goal: transform(formValues.navigationGoal),
    data_extraction_goal: transform(formValues.dataExtractionGoal),
    proxy_location: transform(formValues.proxyLocation),
    navigation_payload: transform(formValues.navigationPayload),
    extracted_information_schema: safeParseMaybeJSONString(
      formValues.extractedInformationSchema,
    ),
    totp_identifier: transform(formValues.totpIdentifier),
    error_code_mapping: safeParseMaybeJSONString(formValues.errorCodeMapping),
  };
}

function safeParseMaybeJSONString(payload: unknown) {
  if (typeof payload === "string") {
    try {
      return JSON.parse(payload);
    } catch {
      return payload;
    }
  }
  return payload;
}

function createTaskTemplateRequestObject(values: SavedTaskFormValues) {
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
          default_value: safeParseMaybeJSONString(values.navigationPayload),
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: "Task 1",
          url: values.url,
          navigation_goal: values.navigationGoal,
          data_extraction_goal: values.dataExtractionGoal,
          data_schema: safeParseMaybeJSONString(
            values.extractedInformationSchema,
          ),
          max_steps_per_run: values.maxStepsOverride,
          totp_identifier: values.totpIdentifier,
          error_code_mapping: safeParseMaybeJSONString(values.errorCodeMapping),
          include_action_history_in_verification:
            values.includeActionHistoryInVerification,
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
  const [openWorkflowDefinitionChangeDialogue, setOpenWorkflowDefinitionChangeDialogue] =
    useState(false);
  const [pendingSaveValues, setPendingSaveValues] = useState<SavedTaskFormValues | null>(null);

  const { data: organizations } = useQuery<Array<OrganizationApiResponse>>({
    queryKey: ["organizations"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return await client
        .get("/organizations")
        .then((response) => response.data.organizations);
    },
  });

  // Fetch the original workflow to compare for definition changes
  const { data: originalWorkflow } = useQuery({
    queryKey: ["savedTasks", template],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/workflows/${template}`)
        .then((response) => response.data);
    },
    enabled: !!template,
    refetchOnWindowFocus: false,
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
        queryKey: ["savedTasks"],
      });
    },
  });

  function handleCreate(values: SavedTaskFormValues) {
    createAndSaveTaskMutation.mutate(values);
  }

  function checkWorkflowDefinitionChanged(values: SavedTaskFormValues): boolean {
    if (!originalWorkflow) {
      return false;
    }

    const normalizeDefinition = (def: any) => {
      const fieldsToRemove = [
        "created_at",
        "modified_at",
        "deleted_at",
        "output_parameter_id",
        "workflow_id",
        "workflow_parameter_id",
      ];
      
      const removeFields = (obj: any): any => {
        if (Array.isArray(obj)) {
          return obj.map(removeFields);
        } else if (obj !== null && typeof obj === "object") {
          const newObj: any = {};
          for (const [key, value] of Object.entries(obj)) {
            if (!fieldsToRemove.includes(key)) {
              newObj[key] = removeFields(value);
            }
          }
          return newObj;
        }
        return obj;
      };

      return JSON.stringify(removeFields(def));
    };

    const newDefinition = createTaskTemplateRequestObject(values).workflow_definition;
    const originalDefinition = originalWorkflow.workflow_definition;

    return normalizeDefinition(newDefinition) !== normalizeDefinition(originalDefinition);
  }

  function handleSave(values: SavedTaskFormValues) {
    const definitionChanged = checkWorkflowDefinitionChanged(values);
    if (definitionChanged) {
      setPendingSaveValues(values);
      setOpenWorkflowDefinitionChangeDialogue(true);
      return;
    }
    saveTaskMutation.mutate(values);
  }

  function confirmSave() {
    if (pendingSaveValues) {
      saveTaskMutation.mutate(pendingSaveValues);
      setPendingSaveValues(null);
      setOpenWorkflowDefinitionChangeDialogue(false);
    }
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
          <CopyApiCommandDropdown
            getOptions={() =>
              ({
                method: "POST",
                url: `${apiBaseUrl}/tasks`,
                body: createTaskRequestObject(form.getValues()),
                headers: {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                },
              }) satisfies ApiCommandOptions
            }
          />
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

      {/* workflow definition change warning dialog */}
      <Dialog
        open={openWorkflowDefinitionChangeDialogue}
        onOpenChange={(open) => {
          if (!open && saveTaskMutation.isPending) {
            return;
          }
          setOpenWorkflowDefinitionChangeDialogue(open);
          if (!open) {
            setPendingSaveValues(null);
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Workflow Definition Changed</DialogTitle>
            <DialogDescription>
              <div className="pb-2 pt-4 text-sm text-slate-400">
                {saveTaskMutation.isPending ? (
                  <>
                    Saving changes and deleting published workflow scripts...
                    <AnimatedWave text=".‧₊˚ ⋅ ✨★ ‧₊˚ ⋅" />
                  </>
                ) : (
                  <div className="flex flex-col gap-3">
                    <p>
                      You have made changes to the task template definition.
                    </p>
                    <p className="font-semibold text-orange-400">
                      All published workflow scripts will be deleted when you save these changes.
                    </p>
                    <p>
                      This ensures generated code stays in sync with your task template structure.
                      Do you want to continue?
                    </p>
                  </div>
                )}
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            {!saveTaskMutation.isPending && (
              <DialogClose asChild>
                <Button variant="secondary">Cancel</Button>
              </DialogClose>
            )}
            <Button
              variant="default"
              onClick={confirmSave}
              disabled={saveTaskMutation.isPending}
            >
              {saveTaskMutation.isPending ? (
                <>
                  Saving...
                  <ReloadIcon className="ml-2 size-4 animate-spin" />
                </>
              ) : (
                "Save and Delete Scripts"
              )}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Form>
  );
}

export { SavedTaskForm };
