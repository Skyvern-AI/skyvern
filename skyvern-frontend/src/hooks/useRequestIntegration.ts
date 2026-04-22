import { useMutation } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "./useCredentialGetter";

export type RequestIntegrationBody = {
  integration_id: string;
  integration_name: string;
  note?: string;
};

export function useRequestIntegration() {
  const credentialGetter = useCredentialGetter();

  const mutation = useMutation({
    mutationFn: async (data: RequestIntegrationBody) => {
      const client = await getClient(credentialGetter);
      return await client.post("/integrations/request", data);
    },
  });

  return {
    requestIntegration: mutation.mutateAsync,
    isRequesting: mutation.isPending,
  };
}
