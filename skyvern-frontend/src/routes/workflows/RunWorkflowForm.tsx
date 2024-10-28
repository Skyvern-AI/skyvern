import { getClient } from "@/api/AxiosClient";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useParams } from "react-router-dom";
import { WorkflowParameterInput } from "./WorkflowParameterInput";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { CopyIcon, PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import { ToastAction } from "@radix-ui/react-toast";
import fetchToCurl from "fetch-to-curl";
import { apiBaseUrl } from "@/util/env";
import { useApiCredential } from "@/hooks/useApiCredential";
import { copyText } from "@/util/copyText";
import { WorkflowParameter } from "./types/workflowTypes";
import { Input } from "@/components/ui/input";
import { z } from "zod";

type Props = {
  workflowParameters: Array<WorkflowParameter>;
  initialValues: Record<string, unknown>;
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

function RunWorkflowForm({ workflowParameters, initialValues }: Props) {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const form = useForm<Record<string, unknown>>({
    defaultValues: { ...initialValues, webhookCallbackUrl: null },
  });
  const apiCredential = useApiCredential();

  const runWorkflowMutation = useMutation({
    mutationFn: async (values: Record<string, unknown>) => {
      const client = await getClient(credentialGetter);

      const { webhookCallbackUrl, ...parameters } = values;

      const body: {
        data: Record<string, unknown>;
        proxy_location: string;
        webhook_callback_url?: string;
      } = {
        data: parameters,
        proxy_location: "RESIDENTIAL",
      };

      if (webhookCallbackUrl) {
        body.webhook_callback_url = webhookCallbackUrl as string;
      }

      return client.post<unknown, { data: { workflow_run_id: string } }>(
        `/workflows/${workflowPermanentId}/run`,
        body,
      );
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "Workflow run started",
        description: "The workflow run has been started successfully",
        action: (
          <ToastAction altText="View">
            <Button asChild>
              <Link
                to={`/workflows/${workflowPermanentId}/${response.data.workflow_run_id}`}
              >
                View
              </Link>
            </Button>
          </ToastAction>
        ),
      });
      queryClient.invalidateQueries({
        queryKey: ["workflowRuns"],
      });
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Failed to start workflow run",
        description: error.message,
      });
    },
  });

  function onSubmit(values: Record<string, unknown>) {
    const parsedValues = parseValuesForWorkflowRun(values, workflowParameters);
    runWorkflowMutation.mutate(parsedValues);
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
                                {parameter.workflow_parameter_type}
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
            name={"webhookCallbackUrl"}
            rules={{
              validate: (value) => {
                if (value === null) {
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
        </div>

        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="secondary"
            onClick={() => {
              const parsedValues = parseValuesForWorkflowRun(
                form.getValues(),
                workflowParameters,
              );
              const curl = fetchToCurl({
                method: "POST",
                url: `${apiBaseUrl}/workflows/${workflowPermanentId}/run`,
                body: {
                  data: parsedValues,
                  proxy_location: "RESIDENTIAL",
                },
                headers: {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                },
              });
              copyText(curl).then(() => {
                toast({
                  variant: "success",
                  title: "Copied to Clipboard",
                  description:
                    "The cURL command has been copied to your clipboard.",
                });
              });
            }}
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            cURL
          </Button>
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
