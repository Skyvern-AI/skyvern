import { AxiosError } from "axios";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useEffect, useState } from "react";
import { type FieldErrors, useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { z } from "zod";

import { getClient } from "@/api/AxiosClient";
import { ProxyLocation } from "@/api/types";
import { ProxySelector } from "@/components/ProxySelector";
import { Button } from "@/components/ui/button";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { CopyApiCommandDropdown } from "@/components/CopyApiCommandDropdown";
import { Input } from "@/components/ui/input";
import { KeyValueInput } from "@/components/KeyValueInput";
import { toast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useBlockScriptsQuery } from "@/routes/workflows/hooks/useBlockScriptsQuery";
import { constructCacheKeyValueFromParameters } from "@/routes/workflows/editor/utils";
import { useWorkflowQuery } from "@/routes/workflows/hooks/useWorkflowQuery";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { runsApiBaseUrl } from "@/util/env";

import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "./editor/nodes/Taskv2Node/types";
import { getLabelForWorkflowParameterType } from "./editor/workflowEditorUtils";
import { WorkflowParameter } from "./types/workflowTypes";
import { WorkflowParameterInput } from "./WorkflowParameterInput";
import { TestWebhookDialog } from "@/components/TestWebhookDialog";
import * as env from "@/util/env";

// Utility function to omit specified keys from an object
function omit<T extends Record<string, unknown>, K extends keyof T>(
  obj: T,
  keys: K[],
): Omit<T, K> {
  const result = { ...obj };
  keys.forEach((key) => delete result[key]);
  return result;
}

type Props = {
  workflowParameters: Array<WorkflowParameter>;
  initialValues: Record<string, unknown>;
  initialSettings: {
    proxyLocation: ProxyLocation;
    webhookCallbackUrl: string;
    cdpAddress: string | null;
    maxScreenshotScrolls: number | null;
    extraHttpHeaders: Record<string, string> | null;
  };
};

function parseValuesForWorkflowRun(
  values: Record<string, unknown>,
  workflowParameters: Array<WorkflowParameter>,
): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(values).map(([key, value]) => {
      const parameter = workflowParameters?.find(
        (parameter) => parameter.key === key,
      );
      if (parameter?.workflow_parameter_type === "json") {
        try {
          return [key, JSON.parse(value as string)];
        } catch {
          console.error("Invalid JSON"); // this should never happen, it should fall to form error
          return [key, value];
        }
      }
      // can improve this via the type system maybe
      if (
        parameter?.workflow_parameter_type === "file_url" &&
        value !== null &&
        typeof value === "object" &&
        "s3uri" in value
      ) {
        return [key, value.s3uri];
      }
      // Convert boolean values to strings for backend storage
      if (
        parameter?.workflow_parameter_type === "boolean" &&
        typeof value === "boolean"
      ) {
        return [key, String(value)];
      }
      if (parameter?.workflow_parameter_type === "string") {
        if (value === null || value === undefined) {
          return [key, ""];
        }
        return [key, String(value)];
      }

      if (
        parameter?.workflow_parameter_type === "integer" ||
        parameter?.workflow_parameter_type === "float"
      ) {
        if (
          value === null ||
          value === undefined ||
          (typeof value === "number" && Number.isNaN(value))
        ) {
          return [key, ""];
        }
        return [key, String(value)];
      }

      return [key, value];
    }),
  );
}

type RunWorkflowRequestBody = {
  data: Record<string, unknown>; // workflow parameters and values
  proxy_location: ProxyLocation | null;
  webhook_callback_url?: string | null;
  browser_session_id: string | null;
  max_screenshot_scrolls?: number | null;
  extra_http_headers?: Record<string, string> | null;
  browser_address?: string | null;
  run_with?: "agent" | "code";
  ai_fallback?: boolean;
};

function getRunWorkflowRequestBody(
  values: RunWorkflowFormType,
  workflowParameters: Array<WorkflowParameter>,
): RunWorkflowRequestBody {
  const {
    webhookCallbackUrl,
    proxyLocation,
    browserSessionId,
    cdpAddress,
    maxScreenshotScrolls,
    extraHttpHeaders,
    runWithCode,
    aiFallback,
    ...parameters
  } = values;

  const parsedParameters = parseValuesForWorkflowRun(
    parameters,
    workflowParameters,
  );

  const bsi = browserSessionId?.trim() === "" ? null : browserSessionId;

  const body: RunWorkflowRequestBody = {
    data: parsedParameters,
    proxy_location: proxyLocation,
    browser_session_id: bsi,
    browser_address: cdpAddress,
    run_with: runWithCode === true ? "code" : "agent",
    ai_fallback: aiFallback ?? true,
  };

  if (maxScreenshotScrolls) {
    body.max_screenshot_scrolls = maxScreenshotScrolls;
  }

  if (webhookCallbackUrl) {
    body.webhook_callback_url = webhookCallbackUrl;
  }

  if (extraHttpHeaders) {
    try {
      body.extra_http_headers = JSON.parse(extraHttpHeaders);
    } catch (e) {
      console.error("Invalid extra Header JSON");
      body.extra_http_headers = null;
    }
  }

  return body;
}

// Transform RunWorkflowRequestBody to match WorkflowRunRequest schema for Runs API v2
function transformToWorkflowRunRequest(
  body: RunWorkflowRequestBody,
  workflowId: string,
) {
  const { data, webhook_callback_url, ...rest } = body;
  const transformed: Record<string, unknown> = {
    workflow_id: workflowId,
    parameters: data,
    ...rest,
  };

  if (webhook_callback_url) {
    transformed.webhook_url = webhook_callback_url;
  }

  return transformed;
}

type RunWorkflowFormType = Record<string, unknown> & {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  browserSessionId: string | null;
  cdpAddress: string | null;
  maxScreenshotScrolls: number | null;
  extraHttpHeaders: string | null;
  runWithCode: boolean | null;
  aiFallback: boolean | null;
};

function RunWorkflowForm({
  workflowParameters,
  initialValues,
  initialSettings,
}: Props) {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const apiCredential = useApiCredential();
  const { data: workflow } = useWorkflowQuery({ workflowPermanentId });

  const form = useForm<RunWorkflowFormType>({
    mode: "onTouched",
    reValidateMode: "onChange",
    defaultValues: {
      ...initialValues,
      webhookCallbackUrl: initialSettings.webhookCallbackUrl,
      proxyLocation: initialSettings.proxyLocation ?? ProxyLocation.Residential,
      browserSessionId: null,
      cdpAddress: initialSettings.cdpAddress,
      maxScreenshotScrolls: initialSettings.maxScreenshotScrolls,
      extraHttpHeaders: initialSettings.extraHttpHeaders
        ? JSON.stringify(initialSettings.extraHttpHeaders)
        : null,
      runWithCode: workflow?.run_with === "code",
      aiFallback: workflow?.ai_fallback ?? true,
    },
  });

  const runWorkflowMutation = useMutation({
    mutationFn: async (values: RunWorkflowFormType) => {
      const client = await getClient(credentialGetter);
      const body = getRunWorkflowRequestBody(values, workflowParameters);
      return client.post<
        RunWorkflowRequestBody,
        { data: { workflow_run_id: string } }
      >(`/workflows/${workflowPermanentId}/run`, body);
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "Workflow run started",
        description: "The workflow run has been started successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflowRuns"],
      });
      queryClient.invalidateQueries({
        queryKey: ["runs"],
      });
      navigate(
        env.useNewRunsUrl
          ? `/runs/${response.data.workflow_run_id}`
          : `/workflows/${workflowPermanentId}/${response.data.workflow_run_id}/overview`,
      );
    },
    onError: (error: AxiosError) => {
      const detail = (error.response?.data as { detail?: string })?.detail;
      toast({
        variant: "destructive",
        title: "Failed to start workflow run",
        description: detail ?? error.message,
      });
    },
  });

  const [runParameters, setRunParameters] = useState<Record<
    string,
    unknown
  > | null>(null);
  const [cacheKeyValue, setCacheKeyValue] = useState<string>("");
  const [isFormReset, setIsFormReset] = useState(false);
  const cacheKey = workflow?.cache_key ?? "default";

  useEffect(() => {
    if (!runParameters) {
      setCacheKeyValue("");
      return;
    }

    const ckv = constructCacheKeyValueFromParameters({
      codeKey: cacheKey,
      parameters: runParameters,
    });

    setCacheKeyValue(ckv);
  }, [cacheKey, runParameters]);

  const { data: blockScripts } = useBlockScriptsQuery({
    cacheKey,
    cacheKeyValue,
    workflowPermanentId,
    status: "published",
  });

  const [hasCode, setHasCode] = useState(false);

  useEffect(() => {
    setHasCode(Object.keys(blockScripts ?? {}).length > 0);
  }, [blockScripts]);

  // Watch form changes and update run parameters without triggering validation
  useEffect(() => {
    const subscription = form.watch((values) => {
      onChange(values as RunWorkflowFormType);
    });
    return () => subscription.unsubscribe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reset form with initial values after all fields are registered
  useEffect(() => {
    form.reset({
      ...initialValues,
      webhookCallbackUrl: initialSettings.webhookCallbackUrl,
      proxyLocation: initialSettings.proxyLocation ?? ProxyLocation.Residential,
      browserSessionId: null,
      cdpAddress: initialSettings.cdpAddress,
      maxScreenshotScrolls: initialSettings.maxScreenshotScrolls,
      extraHttpHeaders: initialSettings.extraHttpHeaders
        ? JSON.stringify(initialSettings.extraHttpHeaders)
        : null,
      runWithCode: workflow?.run_with === "code",
      aiFallback: workflow?.ai_fallback ?? true,
    });
    setIsFormReset(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Trigger validation after form is reset and re-rendered
  useEffect(() => {
    if (isFormReset) {
      form.trigger();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isFormReset]);

  // if we're coming from debugger, block scripts may already be cached; let's ensure we bust it
  // on mount
  useEffect(() => {
    queryClient.invalidateQueries({
      queryKey: ["block-scripts"],
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function onSubmit(values: RunWorkflowFormType) {
    const {
      webhookCallbackUrl,
      proxyLocation,
      browserSessionId,
      maxScreenshotScrolls,
      extraHttpHeaders,
      cdpAddress,
      runWithCode,
      aiFallback,
      ...parameters
    } = values;

    const parsedParameters = parseValuesForWorkflowRun(
      parameters,
      workflowParameters,
    );
    runWorkflowMutation.mutate({
      ...parsedParameters,
      webhookCallbackUrl,
      proxyLocation,
      browserSessionId,
      maxScreenshotScrolls,
      extraHttpHeaders,
      cdpAddress,
      runWithCode,
      aiFallback,
    });
  }

  function onChange(values: RunWorkflowFormType) {
    const parameters = omit(values, [
      "webhookCallbackUrl",
      "proxyLocation",
      "browserSessionId",
      "maxScreenshotScrolls",
      "extraHttpHeaders",
      "cdpAddress",
      "runWithCode",
    ]);

    const parsedParameters = parseValuesForWorkflowRun(
      parameters,
      workflowParameters,
    );

    setRunParameters(parsedParameters);
  }

  const handleInvalid = (errors: FieldErrors<RunWorkflowFormType>) => {
    const hasBlockingErrors = workflowParameters.some(
      (param) =>
        (param.workflow_parameter_type === "boolean" ||
          param.workflow_parameter_type === "integer" ||
          param.workflow_parameter_type === "float" ||
          param.workflow_parameter_type === "file_url" ||
          param.workflow_parameter_type === "json") &&
        errors[param.key],
    );

    if (!hasBlockingErrors) {
      onSubmit(form.getValues());
    }
  };

  if (!workflowPermanentId || !workflow) {
    return <div>Invalid workflow</div>;
  }

  return (
    <Form {...form}>
      <form
        onSubmit={form.handleSubmit(onSubmit, handleInvalid)}
        className="space-y-8"
      >
        <div className="space-y-8 rounded-lg bg-slate-elevation3 px-6 py-5">
          <header>
            <h1 className="text-lg">Input Parameters</h1>
          </header>
          {workflowParameters?.map((parameter) => {
            return (
              <FormField
                key={parameter.key}
                control={form.control}
                name={parameter.key}
                rules={{
                  validate: (value) => {
                    if (parameter.workflow_parameter_type === "json") {
                      if (value === null || value === undefined) {
                        return "This field is required";
                      }
                      if (typeof value === "string") {
                        const trimmed = value.trim();
                        if (trimmed === "") {
                          return "This field is required";
                        }
                        try {
                          JSON.parse(trimmed);
                          return true;
                        } catch (e) {
                          return "Invalid JSON";
                        }
                      }
                      return;
                    }

                    // Boolean parameters are required - show error and block submission
                    if (parameter.workflow_parameter_type === "boolean") {
                      if (value === null || value === undefined) {
                        return "This field is required";
                      }
                      return;
                    }

                    // Numeric parameters are required - show error and block submission
                    if (
                      parameter.workflow_parameter_type === "integer" ||
                      parameter.workflow_parameter_type === "float"
                    ) {
                      if (
                        value === null ||
                        value === undefined ||
                        Number.isNaN(value)
                      ) {
                        return "This field is required";
                      }
                      return;
                    }

                    if (parameter.workflow_parameter_type === "file_url") {
                      if (
                        value === null ||
                        value === undefined ||
                        (typeof value === "string" && value.trim() === "") ||
                        (typeof value === "object" &&
                          value !== null &&
                          "s3uri" in value &&
                          !value.s3uri)
                      ) {
                        return "This field is required";
                      }
                      return;
                    }

                    // For string parameters, show warning but don't block
                    if (
                      parameter.workflow_parameter_type === "string" &&
                      (value === null || value === "")
                    ) {
                      return "Warning: you left this field empty";
                    }

                    // For all other non-boolean types, show warning but don't block
                    if (value === null || value === undefined) {
                      return "Warning: you left this field empty";
                    }
                  },
                }}
                render={({ field }) => {
                  return (
                    <FormItem>
                      <div className="flex gap-16">
                        <FormLabel className="!text-slate-50">
                          <div className="w-72">
                            <div className="flex items-center gap-2 text-lg">
                              {parameter.key}
                              <span className="text-sm text-slate-400">
                                {getLabelForWorkflowParameterType(
                                  parameter.workflow_parameter_type,
                                )}
                              </span>
                            </div>
                            <h2 className="text-sm text-slate-400">
                              {parameter.description}
                            </h2>
                          </div>
                        </FormLabel>
                        <div className="w-full space-y-2">
                          <FormControl>
                            <WorkflowParameterInput
                              type={parameter.workflow_parameter_type}
                              value={field.value}
                              onChange={(value) => {
                                field.onChange(value);
                                form.trigger(parameter.key);
                              }}
                            />
                          </FormControl>
                          {form.formState.errors[parameter.key] && (
                            <div
                              className={`text-xs ${
                                parameter.workflow_parameter_type ===
                                  "boolean" ||
                                parameter.workflow_parameter_type ===
                                  "integer" ||
                                parameter.workflow_parameter_type === "float" ||
                                parameter.workflow_parameter_type ===
                                  "file_url" ||
                                parameter.workflow_parameter_type === "json"
                                  ? "text-destructive"
                                  : "text-warning"
                              }`}
                            >
                              {form.formState.errors[parameter.key]?.message}
                            </div>
                          )}
                        </div>
                      </div>
                    </FormItem>
                  );
                }}
              />
            );
          })}
          {workflowParameters.length === 0 && (
            <div>This workflow doesn't have any input parameters</div>
          )}
        </div>

        <div className="space-y-8 rounded-lg bg-slate-elevation3 px-6 py-5">
          <header>
            <h1 className="text-lg">Settings</h1>
          </header>
          <FormField
            key="webhookCallbackUrl"
            control={form.control}
            name="webhookCallbackUrl"
            rules={{
              validate: (value) => {
                if (value === null || value === "") {
                  return;
                }
                if (typeof value !== "string") {
                  return "Invalid URL";
                }
                const urlSchema = z.string().url({ message: "Invalid URL" });
                const { success } = urlSchema.safeParse(value);
                if (!success) {
                  return "Invalid URL";
                }
              },
            }}
            render={({ field }) => {
              return (
                <FormItem>
                  <div className="flex gap-16">
                    <FormLabel>
                      <div className="w-72">
                        <div className="flex items-center gap-2 text-lg">
                          Webhook Callback URL
                        </div>
                        <h2 className="text-sm text-slate-400">
                          The URL of a webhook endpoint to send the details of
                          the workflow result.
                        </h2>
                      </div>
                    </FormLabel>
                    <div className="w-full space-y-2">
                      <FormControl>
                        <div className="flex flex-col gap-2">
                          <Input
                            className="w-full"
                            {...field}
                            placeholder="https://"
                            value={
                              field.value === null
                                ? ""
                                : (field.value as string)
                            }
                          />
                          <TestWebhookDialog
                            runType="workflow_run"
                            runId={null}
                            initialWebhookUrl={
                              field.value === null
                                ? undefined
                                : (field.value as string)
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
              );
            }}
          />
          <FormField
            key="proxyLocation"
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
                          Route Skyvern through one of our available proxies.
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
            key="runWithCode"
            control={form.control}
            name="runWithCode"
            render={({ field }) => {
              return (
                <FormItem>
                  <div className="flex gap-16">
                    <FormLabel>
                      <div className="w-72">
                        <div className="flex items-center gap-2 text-lg">
                          Run With
                        </div>
                        <h2 className="text-sm text-slate-400">
                          {field.value ? (
                            hasCode ? (
                              <span>
                                Run this workflow with generated code.
                              </span>
                            ) : (
                              <span>
                                Run this workflow with generated code (after it
                                is first generated).
                              </span>
                            )
                          ) : hasCode ? (
                            <span>
                              Run this workflow with AI. (Even though it has
                              generated code.)
                            </span>
                          ) : (
                            <span>Run this workflow with AI.</span>
                          )}
                        </h2>
                      </div>
                    </FormLabel>
                    <div className="w-full space-y-2">
                      <FormControl>
                        <Select
                          value={field.value ? "code" : "ai"}
                          onValueChange={(v) =>
                            field.onChange(v === "code" ? true : false)
                          }
                        >
                          <SelectTrigger className="w-48">
                            <SelectValue placeholder="Run Method" />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="ai">Skyvern Agent</SelectItem>
                            <SelectItem value="code">Code</SelectItem>
                          </SelectContent>
                        </Select>
                      </FormControl>
                      <FormMessage />
                    </div>
                  </div>
                </FormItem>
              );
            }}
          />

          <FormField
            key="aiFallback"
            control={form.control}
            name="aiFallback"
            render={({ field }) => {
              return (
                <FormItem>
                  <div className="flex gap-16">
                    <FormLabel>
                      <div className="w-72">
                        <div className="flex items-center gap-2 text-lg">
                          AI Fallback (self-healing)
                        </div>
                        <h2 className="text-sm text-slate-400">
                          If the run fails when running with code, keep this on
                          to have AI attempt to fix the issue and regenerate the
                          code.
                        </h2>
                      </div>
                    </FormLabel>
                    <div className="w-full space-y-2">
                      <FormControl>
                        <Switch
                          checked={field.value ?? true}
                          onCheckedChange={field.onChange}
                        />
                      </FormControl>
                      <FormMessage />
                    </div>
                  </div>
                </FormItem>
              );
            }}
          />
        </div>

        <div className="space-y-8 rounded-lg bg-slate-elevation3 px-6 py-5">
          <Accordion type="single" collapsible>
            <AccordionItem value="advanced" className="border-b-0">
              <AccordionTrigger className="py-0">
                <header>
                  <h1 className="text-lg">Advanced Settings</h1>
                </header>
              </AccordionTrigger>
              <AccordionContent className="pl-6 pr-1 pt-1">
                <div className="space-y-8 pt-5">
                  <FormField
                    key="browserSessionId"
                    control={form.control}
                    name="browserSessionId"
                    render={({ field }) => {
                      return (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <div className="flex items-center gap-2 text-lg">
                                  Browser Session ID
                                </div>
                                <h2 className="text-sm text-slate-400">
                                  Use a persistent browser session to maintain
                                  state and enable browser interaction.
                                </h2>
                              </div>
                            </FormLabel>
                            <div className="w-full space-y-2">
                              <FormControl>
                                <Input
                                  {...field}
                                  placeholder="pbs_xxx"
                                  value={
                                    field.value === null
                                      ? ""
                                      : (field.value as string)
                                  }
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
                    key="cdpAddress"
                    control={form.control}
                    name="cdpAddress"
                    render={({ field }) => {
                      return (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <div className="flex items-center gap-2 text-lg">
                                  Browser Address
                                </div>
                                <h2 className="text-sm text-slate-400">
                                  The address of the Browser server to use for
                                  the workflow run.
                                </h2>
                              </div>
                            </FormLabel>
                            <div className="w-full space-y-2">
                              <FormControl>
                                <Input
                                  {...field}
                                  placeholder="http://127.0.0.1:9222"
                                  value={
                                    field.value === null
                                      ? ""
                                      : (field.value as string)
                                  }
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
                    key="extraHttpHeaders"
                    control={form.control}
                    name="extraHttpHeaders"
                    render={({ field }) => {
                      return (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <div className="flex items-center gap-2 text-lg">
                                  Extra HTTP Headers
                                </div>
                                <h2 className="text-sm text-slate-400">
                                  Specify some self defined HTTP requests
                                  headers in Dict format
                                </h2>
                              </div>
                            </FormLabel>
                            <div className="w-full space-y-2">
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
                      );
                    }}
                  />
                  <FormField
                    key="maxScreenshotScrolls"
                    control={form.control}
                    name="maxScreenshotScrolls"
                    render={({ field }) => {
                      return (
                        <FormItem>
                          <div className="flex gap-16">
                            <FormLabel>
                              <div className="w-72">
                                <div className="flex items-center gap-2 text-lg">
                                  Max Screenshot Scrolls
                                </div>
                                <h2 className="text-sm text-slate-400">
                                  {`The maximum number of scrolls for the post action screenshot. Default is ${MAX_SCREENSHOT_SCROLLS_DEFAULT}. If it's set to 0, it will take the current viewport screenshot.`}
                                </h2>
                              </div>
                            </FormLabel>
                            <div className="w-full space-y-2">
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
                      );
                    }}
                  />
                </div>
              </AccordionContent>
            </AccordionItem>
          </Accordion>
        </div>

        <div className="flex justify-end gap-2">
          <CopyApiCommandDropdown
            getOptions={() => {
              const values = form.getValues();
              const body = getRunWorkflowRequestBody(
                values,
                workflowParameters,
              );
              const transformedBody = transformToWorkflowRunRequest(
                body,
                workflowPermanentId,
              );

              // Build headers - x-max-steps-override is optional and can be added manually if needed
              const headers: Record<string, string> = {
                "Content-Type": "application/json",
                "x-api-key": apiCredential ?? "<your-api-key>",
              };

              return {
                method: "POST",
                url: `${runsApiBaseUrl}/run/workflows`,
                body: transformedBody,
                headers,
              } satisfies ApiCommandOptions;
            }}
          />
          <Button type="submit" disabled={runWorkflowMutation.isPending}>
            {runWorkflowMutation.isPending && (
              <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
            )}
            {!runWorkflowMutation.isPending && (
              <PlayIcon className="mr-2 h-4 w-4" />
            )}
            Run workflow
          </Button>
        </div>
      </form>
    </Form>
  );
}

export { RunWorkflowForm };
