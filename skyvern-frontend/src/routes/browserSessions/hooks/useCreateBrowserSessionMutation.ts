import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  BrowserSession,
  BrowserSessionExtension,
  BrowserSessionType,
} from "@/routes/workflows/types/browserSessionTypes";
import { ProxyLocation } from "@/api/types";

function useCreateBrowserSessionMutation() {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();

  return useMutation({
    mutationFn: async ({
      proxyLocation = null,
      timeout = null,
      extensions = [],
      browserType = null,
    }: {
      proxyLocation: ProxyLocation | null;
      timeout: number | null;
      extensions?: BrowserSessionExtension[];
      browserType?: BrowserSessionType | null;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post<string, { data: BrowserSession }>(
        "/browser_sessions",
        {
          proxy_location: proxyLocation,
          timeout,
          extensions,
          browser_type: browserType,
        },
      );
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["browser_sessions"],
      });
      navigate(`/browser-session/${response.data.browser_session_id}`);
    },
    onError: (error: unknown) => {
      let errorMessage =
        "Browser session could not be started. Please try again.";
      if (error && typeof error === "object") {
        const axiosError = error as {
          response?: { data?: { detail?: string } };
          message?: string;
        };
        if (axiosError.response?.data?.detail) {
          errorMessage = axiosError.response.data.detail;
        } else if (axiosError.message) {
          errorMessage = axiosError.message;
        }
      }
      toast({
        variant: "destructive",
        title: "Failed to create browser session",
        description: errorMessage,
      });
    },
  });
}

export { useCreateBrowserSessionMutation };
