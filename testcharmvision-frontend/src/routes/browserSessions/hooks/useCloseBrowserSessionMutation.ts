import { useMutation, useQueryClient } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

const useCloseBrowserSessionMutation = ({
  browserSessionId,
  onSuccess,
}: {
  browserSessionId?: string;
  onSuccess?: () => void;
}) => {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const closeBrowserSessionMutation = useMutation({
    mutationFn: async () => {
      if (!browserSessionId) {
        console.warn("No browserSessionId provided for close mutation");
        return;
      }

      const client = await getClient(credentialGetter, "sans-api-v1");

      return client
        .post(`/browser_sessions/${browserSessionId}/close`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["browserSessions"],
      });
      queryClient.invalidateQueries({
        queryKey: ["browserSession", browserSessionId],
      });
      toast({
        variant: "success",
        title: "Browser Session Closed",
        description: "The browser session has been successfully closed.",
      });
      onSuccess?.();
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
  });

  return closeBrowserSessionMutation;
};

export { useCloseBrowserSessionMutation };
