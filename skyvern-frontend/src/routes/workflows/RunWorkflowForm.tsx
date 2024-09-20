import { getClient } from "@/api/AxiosClient";
import { Form, FormControl, FormField, FormItem } from "@/components/ui/form";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { Link, useParams } from "react-router-dom";
import { WorkflowParameterInput } from "./WorkflowParameterInput";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { CopyIcon, PlayIcon, ReloadIcon } from "@radix-ui/react-icons";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ToastAction } from "@radix-ui/react-toast";
import fetchToCurl from "fetch-to-curl";
import { apiBaseUrl } from "@/util/env";
import { useApiCredential } from "@/hooks/useApiCredential";
import { copyText } from "@/util/copyText";
import { WorkflowParameter } from "./types/workflowTypes";

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
  const form = useForm({
    defaultValues: initialValues,
  });
  const apiCredential = useApiCredential();

  const runWorkflowMutation = useMutation({
    mutationFn: async (values: Record<string, unknown>) => {
      const client = await getClient(credentialGetter);
      return client.post<unknown, { data: { workflow_run_id: string } }>(
        `/workflows/${workflowPermanentId}/run`,
        {
          data: values,
          proxy_location: "RESIDENTIAL",
        },
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
    <div>
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-8">
          <Table>
            <TableHeader className="bg-slate-elevation2 text-slate-400 [&_tr]:border-b-0">
              <TableRow className="rounded-lg px-6 [&_th:first-child]:pl-6 [&_th]:py-4">
                <TableHead className="w-1/3 text-sm text-slate-400">
                  Parameter Name
                </TableHead>
                <TableHead className="w-1/3 text-sm text-slate-400">
                  Description
                </TableHead>
                <TableHead className="w-1/3 text-sm text-slate-400">
                  Input
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
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
                        <TableRow className="[&_td:first-child]:pl-6 [&_td:last-child]:pr-6 [&_td]:py-4">
                          <TableCell className="w-1/3">
                            <div className="flex h-8 w-fit items-center rounded-sm bg-slate-elevation3 p-3">
                              {parameter.key}
                            </div>
                          </TableCell>
                          <TableCell className="w-1/3">
                            <div>{parameter.description}</div>
                          </TableCell>
                          <TableCell className="w-1/3">
                            <FormItem>
                              <FormControl>
                                <WorkflowParameterInput
                                  type={parameter.workflow_parameter_type}
                                  value={field.value}
                                  onChange={field.onChange}
                                />
                              </FormControl>
                              {form.formState.errors[parameter.key] && (
                                <div className="text-destructive">
                                  {
                                    form.formState.errors[parameter.key]
                                      ?.message
                                  }
                                </div>
                              )}
                            </FormItem>
                          </TableCell>
                        </TableRow>
                      );
                    }}
                  />
                );
              })}
            </TableBody>
          </Table>
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
              Copy as cURL
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
    </div>
  );
}

export { RunWorkflowForm };
