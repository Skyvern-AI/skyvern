import { useQueryClient, useMutation } from "@tanstack/react-query";
import { WorkflowCreateYAMLRequest } from "../types/workflowYamlTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { getClient } from "@/api/AxiosClient";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { toast } from "@/components/ui/use-toast";
import { OnboardingTelemetry } from "@/util/onboarding/OnboardingTelemetry";
import { useWorkflowStudioEnabled } from "@/hooks/useWorkflowStudioEnabled";
import { workflowEditorPath } from "../studioNavigation";
import axios from "axios";
import { HomeTelemetry, type AgentCreationAttempt } from "@/util/homeTelemetry";

type CreateWorkflowInput = WorkflowCreateYAMLRequest & {
  _via?: string;
  _agentCreationAttempt?: AgentCreationAttempt;
};

type UseCreateWorkflowMutationOptions = {
  onCreated?: () => void;
};

function hasWorkflowShape(data: unknown): data is WorkflowApiResponse {
  if (typeof data !== "object" || data === null) {
    return false;
  }
  const candidate = data as Partial<WorkflowApiResponse>;
  const definition = candidate.workflow_definition;
  return (
    typeof candidate.workflow_permanent_id === "string" &&
    typeof definition === "object" &&
    definition !== null &&
    Array.isArray(definition.blocks)
  );
}

function getCreateWorkflowErrorMessage(error: unknown) {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === "string") {
      return detail;
    }
    return error.message;
  }

  if (error instanceof Error) {
    return error.message;
  }

  return "Please try again.";
}

function useCreateWorkflowMutation({
  onCreated,
}: UseCreateWorkflowMutationOptions = {}) {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const studioEnabled = useWorkflowStudioEnabled();
  return useMutation({
    mutationFn: async (input: CreateWorkflowInput) => {
      const { _via: _, _agentCreationAttempt: __, ...workflow } = input;
      void _;
      void __;
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(workflow);
      const response = await client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
      if (!hasWorkflowShape(response.data)) {
        throw new Error("The server returned an invalid workflow response.");
      }
      return response;
    },
    onSuccess: (response, variables) => {
      if (variables._agentCreationAttempt) {
        HomeTelemetry.agentCreationSucceeded(
          variables._agentCreationAttempt,
          response.data.workflow_permanent_id,
        );
      }
      onCreated?.();
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      queryClient.invalidateQueries({
        queryKey: ["folders"],
      });
      queryClient.invalidateQueries({
        queryKey: ["userOnboarding"],
      });
      const via = variables._via;
      // Emit completion here rather than in the caller's mutate-level onSuccess:
      // this navigation unmounts the onboarding modal, and React Query skips
      // mutate-level callbacks once their observer unmounts.
      if (via === "onboarding_template") {
        OnboardingTelemetry.flowCompleted("discover");
      }
      const search = via ? `?via=${encodeURIComponent(via)}` : "";
      navigate(
        workflowEditorPath(
          response.data.workflow_permanent_id,
          studioEnabled,
          search,
        ),
      );
    },
    onError: (error, variables) => {
      if (variables._agentCreationAttempt) {
        HomeTelemetry.agentCreationFailed(
          variables._agentCreationAttempt,
          error,
        );
      }
      toast({
        variant: "destructive",
        title: "Could not create agent",
        description: getCreateWorkflowErrorMessage(error),
      });
    },
  });
}

export { useCreateWorkflowMutation };
