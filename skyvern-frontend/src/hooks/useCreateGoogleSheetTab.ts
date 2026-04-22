import { useMutation, useQueryClient } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useToast } from "@/components/ui/use-toast";
import { extractSpreadsheetIdFromUrl } from "@/util/googleSheetsUrl";
import type { CreateGoogleSheetTabResponse, GoogleSheetTab } from "@/api/types";

type Args = {
  credentialId: string;
  spreadsheetUrlOrId: string;
  title: string;
};

export function useCreateGoogleSheetTab() {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  return useMutation<GoogleSheetTab, unknown, Args>({
    mutationFn: async ({ credentialId, spreadsheetUrlOrId, title }) => {
      const spreadsheetId = extractSpreadsheetIdFromUrl(spreadsheetUrlOrId);
      if (!spreadsheetId) {
        throw new Error("Could not resolve spreadsheet id");
      }
      const client = await getClient(credentialGetter);
      const response = await client.post(
        `/google/sheets/spreadsheets/${spreadsheetId}/tabs`,
        { credential_id: credentialId, title },
      );
      return (response.data as CreateGoogleSheetTabResponse).tab;
    },
    onSuccess: (_data, { credentialId, spreadsheetUrlOrId }) => {
      const spreadsheetId = extractSpreadsheetIdFromUrl(spreadsheetUrlOrId);
      queryClient.invalidateQueries({
        queryKey: ["googleSheets", "tabs", credentialId, spreadsheetId],
      });
    },
    onError: (error: unknown) => {
      const detail =
        (error as { response?: { data?: { detail?: string } } })?.response?.data
          ?.detail ??
        (error as Error)?.message ??
        "Failed to create sheet";
      toast({
        title: "Error",
        description: detail,
        variant: "destructive",
      });
    },
  });
}
