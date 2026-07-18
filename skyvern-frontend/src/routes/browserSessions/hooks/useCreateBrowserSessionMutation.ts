import { useNavigate } from "react-router-dom";
import { useQueryClient, useMutation } from "@tanstack/react-query";

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
      proxySessionId = null,
      timeout = null,
      extensions = [],
      browserType = null,
      generateBrowserProfile = false,
      browserProfileId = null,
    }: {
      proxyLocation: ProxyLocation | null;
      proxySessionId?: string | null;
      timeout: number | null;
      extensions?: BrowserSessionExtension[];
      browserType?: BrowserSessionType | null;
      generateBrowserProfile?: boolean;
      browserProfileId?: string | null;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post<string, { data: BrowserSession }>(
        "/browser_sessions",
        {
          proxy_location: proxyLocation,
          proxy_session_id: proxySessionId,
          timeout,
          extensions,
          browser_type: browserType,
          generate_browser_profile: generateBrowserProfile,
          browser_profile_id: browserProfileId,
        },
      );
    },
    onSuccess: (response) => {
      const session = response.data;
      queryClient.setQueryData(
        ["browserSession", session.browser_session_id],
        session,
      );
      queryClient.invalidateQueries({
        queryKey: ["browser_sessions"],
      });
      navigate(`/browser-session/${session.browser_session_id}`);
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
