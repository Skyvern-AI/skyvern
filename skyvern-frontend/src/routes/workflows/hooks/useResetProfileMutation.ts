import { useMutation } from "@tanstack/react-query";
import { isAxiosError } from "axios";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

const useResetProfileMutation = ({
  workflowPermanentId,
  onSuccess,
}: {
  workflowPermanentId?: string;
  onSuccess?: () => void;
}) => {
  const credentialGetter = useCredentialGetter();

  return useMutation({
    mutationFn: async () => {
      if (!workflowPermanentId) {
        throw new Error("workflowPermanentId is required");
      }
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client
        .post(`/workflows/${workflowPermanentId}/browser_session/reset_profile`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      toast({
        variant: "success",
        title: "Profile Reset",
        description:
          "The saved browser profile has been cleared. The next run will start with a fresh session.",
      });
      onSuccess?.();
    },
    onError: (error) => {
      const description = isAxiosError(error)
        ? (error.response?.data?.detail ?? error.message)
        : error.message;
      toast({
        variant: "destructive",
        title: "Failed to Reset Profile",
        description,
      });
    },
  });
};

export { useResetProfileMutation };
