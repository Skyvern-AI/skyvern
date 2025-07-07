import { getClient } from "@/api/AxiosClient";
import { ProxyLocation } from "@/api/types";
import { ProxySelector } from "@/components/ProxySelector";
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
import { KeyValueInput } from "@/components/KeyValueInput";
import { toast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useSyncFormFieldToStorage } from "@/hooks/useSyncFormFieldToStorage";
import { useLocalStorageFormDefault } from "@/hooks/useLocalStorageFormDefault";
import { apiBaseUrl } from "@/util/env";
import { type ApiCommandOptions } from "@/util/apiCommands";
import { PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CopyApiCommandDropdown } from "@/components/CopyApiCommandDropdown";
import { useForm } from "react-hook-form";
import { useNavigate, useParams } from "react-router-dom";
import { z } from "zod";
import { WorkflowParameter } from "./types/workflowTypes";
import { WorkflowParameterInput } from "./WorkflowParameterInput";
import { AxiosError } from "axios";
import { getLabelForWorkflowParameterType } from "./editor/workflowEditorUtils";
import { MAX_SCREENSHOT_SCROLLS_DEFAULT } from "./editor/nodes/Taskv2Node/types";
import { lsKeys } from "@/util/env";

type Props = {
  workflowParameters: Array<WorkflowParameter>;
  initialValues: Record<string, unknown>;
  initialSettings: {
    proxyLocation: ProxyLocation;
    webhookCallbackUrl: string;
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
};

function getRunWorkflowRequestBody(
  values: RunWorkflowFormType,
  workflowParameters: Array<WorkflowParameter>,
): RunWorkflowRequestBody {
  const {
    webhookCallbackUrl,
    proxyLocation,
    browserSessionId,
    maxScreenshotScrolls,
    extraHttpHeaders,
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

type RunWorkflowFormType = Record<string, unknown> & {
  webhookCallbackUrl: string;
  proxyLocation: ProxyLocation;
  browserSessionId: string | null;
  maxScreenshotScrolls: number | null;
  extraHttpHeaders: string | null;
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
  const browserSessionIdDefault = useLocalStorageFormDefault(
    lsKeys.browserSessionId,
    (initialValues.browserSessionId as string | undefined) ?? null,
  );
  const form = useForm<RunWorkflowFormType>({
    defaultValues: {
      ...initialValues,
      webhookCallbackUrl: initialSettings.webhookCallbackUrl,
      proxyLocation: initialSettings.proxyLocation,
      browserSessionId: browserSessionIdDefault,
      maxScreenshotScrolls: initialSettings.maxScreenshotScrolls,
      extraHttpHeaders: initialSettings.extraHttpHeaders
        ? JSON.stringify(initialSettings.extraHttpHeaders)
        : null,
    },
  });
  const apiCredential = useApiCredential();

  useSyncFormFieldToStorage(form, "browserSessionId", lsKeys.browserSessionId);

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
        `/workflows/${workflowPermanentId}/${response.data.workflow_run_id}/overview`,
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

  function onSubmit(values: RunWorkflowFormType) {
    const {
      webhookCallbackUrl,
      proxyLocation,
      browserSessionId,
      maxScreenshotScrolls,
      extraHttpHeaders,
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
    });
  }

  return (
    <Form {...form}>
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-8">
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
                    if (
                      parameter.workflow_parameter_type === "json" &&
                      typeof value === "string"
                    ) {
                      try {
                        JSON.parse(value);
                        return true;
                      } catch (e) {
                        return "Invalid JSON";
                      }
                    }
                    if (value === null) {
                      return "This field is required";
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
                              onChange={field.onChange}
                            />
                          </FormControl>
                          {form.formState.errors[parameter.key] && (
                            <div className="text-destructive">
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
            <h1 className="text-lg">Advanced Settings</h1>
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
                        <Input
                          {...field}
                          placeholder="https://"
                          value={
                            field.value === null ? "" : (field.value as string)
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
                          Use a persistent browser session to maintain state and
                          enable browser interaction.
                        </h2>
                      </div>
                    </FormLabel>
                    <div className="w-full space-y-2">
                      <FormControl>
                        <Input
                          {...field}
                          placeholder="pbs_xxx"
                          value={
                            field.value === null ? "" : (field.value as string)
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
                          Specify some self defined HTTP requests headers in
                          Dict format
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
        <div className="flex justify-end gap-2">
          <CopyApiCommandDropdown
            getOptions={() => {
              const values = form.getValues();
              const body = getRunWorkflowRequestBody(
                values,
                workflowParameters,
              );
              return {
                method: "POST",
                url: `${apiBaseUrl}/workflows/${workflowPermanentId}/run`,
                body,
                headers: {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                },
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
