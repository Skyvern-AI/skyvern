import { getClient } from "@/api/AxiosClient";
import { WorkflowParameter } from "@/api/types";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
} from "@/components/ui/form";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { useParams } from "react-router-dom";
import { WorkflowParameterInput } from "./WorkflowParameterInput";
import { Button } from "@/components/ui/button";
import { toast } from "@/components/ui/use-toast";
import { ReloadIcon } from "@radix-ui/react-icons";

type Props = {
  workflowParameters: Array<WorkflowParameter>;
  initialValues: Record<string, unknown>;
};

function RunWorkflowForm({ workflowParameters, initialValues }: Props) {
  const { workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const form = useForm({
    defaultValues: initialValues,
  });

  const runWorkflowMutation = useMutation({
    mutationFn: async (values: Record<string, unknown>) => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/workflows/${workflowPermanentId}/run`, {
          data: values,
        })
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowRuns"],
      });
      toast({
        variant: "success",
        title: "Workflow run started",
        description: "The workflow run has been started successfully",
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
    const parsedValues = Object.fromEntries(
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
    runWorkflowMutation.mutate(parsedValues);
  }

  return (
    <div>
      <Form {...form}>
        <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-8">
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
                  },
                }}
                render={({ field }) => {
                  return (
                    <FormItem>
                      <FormLabel>{parameter.key}</FormLabel>
                      <FormControl>
                        <WorkflowParameterInput
                          type={parameter.workflow_parameter_type}
                          value={field.value}
                          onChange={field.onChange}
                        />
                      </FormControl>
                      {parameter.description && (
                        <FormDescription>
                          {parameter.description}
                        </FormDescription>
                      )}
                      {form.formState.errors[parameter.key] && (
                        <div className="text-destructive">
                          {form.formState.errors[parameter.key]?.message}
                        </div>
                      )}
                    </FormItem>
                  );
                }}
              />
            );
          })}
          <Button type="submit" disabled={runWorkflowMutation.isPending}>
            {runWorkflowMutation.isPending && (
              <ReloadIcon className="mr-2 w-4 h-4 animate-spin" />
            )}
            Run workflow
          </Button>
        </form>
      </Form>
    </div>
  );
}

export { RunWorkflowForm };
