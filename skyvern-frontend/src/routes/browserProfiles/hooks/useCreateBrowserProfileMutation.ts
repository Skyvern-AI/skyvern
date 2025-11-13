import { getClient } from "@/api/AxiosClient";
import { BrowserProfile } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import { useMutation, useQueryClient } from "@tanstack/react-query";

type CreateBrowserProfileInput = {
  name: string;
  description?: string | null;
  browserSessionId?: string | null;
  workflowRunId?: string | null;
};

function useCreateBrowserProfileMutation() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  return useMutation({
    mutationFn: async (input: CreateBrowserProfileInput) => {
      const client = await getClient(credentialGetter, "sans-api-v1");

      const { browserSessionId, workflowRunId, description, name } = input;
      const hasBrowserSessionId = Boolean(browserSessionId);
      const hasWorkflowRunId = Boolean(workflowRunId);

      console.log("[useCreateBrowserProfileMutation] Input:", input);

      if (hasBrowserSessionId === hasWorkflowRunId) {
        throw new Error(
          "Provide either browserSessionId or workflowRunId when creating a browser profile.",
        );
      }

      const body: Record<string, unknown> = {
        name,
        description: description ?? null,
      };

      if (browserSessionId) {
        body.browser_session_id = browserSessionId;
      } else if (workflowRunId) {
        body.workflow_run_id = workflowRunId;
      }

      console.log("[useCreateBrowserProfileMutation] Request body:", body);

      return client
        .post<BrowserProfile>("/browser_profiles", body)
        .then((response) => response.data);
    },
    onSuccess: (profile) => {
      queryClient.invalidateQueries({ queryKey: ["browserProfiles"] });
      toast({
        title: "Browser profile created",
        description: `Saved profile "${profile.name}".`,
      });
    },
    onError: (error: unknown) => {
      const message =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ||
        (error as Error)?.message ||
        "Failed to create browser profile";

      toast({
        title: "Failed to create browser profile",
        description: message,
        variant: "destructive",
      });
    },
  });
}

export { useCreateBrowserProfileMutation };
