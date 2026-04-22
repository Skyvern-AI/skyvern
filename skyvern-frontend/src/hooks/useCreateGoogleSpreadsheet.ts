import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useToast } from "@/components/ui/use-toast";
import type { CreateGoogleSpreadsheetResponse } from "@/api/types";

type Args = { credentialId: string; title: string };

export function useCreateGoogleSpreadsheet() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  return useMutation<CreateGoogleSpreadsheetResponse, unknown, Args>({
    mutationFn: async ({ credentialId, title }) => {
      const client = await getClient(credentialGetter);
      const response = await client.post("/google/sheets/spreadsheets", {
        credential_id: credentialId,
        title,
      });
      return response.data as CreateGoogleSpreadsheetResponse;
    },
    onSuccess: (_data, { credentialId }) => {
      queryClient.invalidateQueries({
        queryKey: ["googleSheets", "spreadsheets", credentialId],
      });
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ??
        (error as Error)?.message ??
        "Failed to create spreadsheet";
      toast({
        title: "Error",
        description: detail,
        variant: "destructive",
      });
    },
  });
}
