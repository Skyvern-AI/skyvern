import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { useMutation } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { BrowserSession } from "@/routes/workflows/types/browserSessionTypes";

function useCreateBrowserSessionMutation() {
  const queryClient = useQueryClient();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();

  return useMutation({
    mutationFn: async ({
      proxyLocation = null,
      timeout = null,
    }: {
      proxyLocation: string | null;
      timeout: number | null;
    }) => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.post<string, { data: BrowserSession }>(
        "/browser_sessions",
        {
          proxy_location: proxyLocation,
          timeout,
        },
      );
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["browser_sessions"],
      });
      navigate(`/browser-session/${response.data.browser_session_id}`);
    },
  });
}

export { useCreateBrowserSessionMutation };
