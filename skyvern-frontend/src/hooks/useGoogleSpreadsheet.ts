import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { GoogleSpreadsheetSummary } from "@/api/types";
import { isTemplateExpression } from "@/util/googleSheetsUrl";

type Options = {
  credentialId: string;
  spreadsheetId: string | null;
};

export function useGoogleSpreadsheet({ credentialId, spreadsheetId }: Options) {
  const credentialGetter = useCredentialGetter();

  const enabled =
    Boolean(credentialId) &&
    !isTemplateExpression(credentialId) &&
    Boolean(spreadsheetId);

  return useQuery<GoogleSpreadsheetSummary>({
    queryKey: ["googleSheets", "spreadsheet", credentialId, spreadsheetId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get(
        `/google/sheets/spreadsheets/${encodeURIComponent(spreadsheetId as string)}`,
        { params: { credential_id: credentialId } },
      );
      return response.data as GoogleSpreadsheetSummary;
    },
    enabled,
    // Titles change rarely; the block picker already re-queries when the user
    // touches the URL (new id -> new queryKey).
    staleTime: 10 * 60_000,
    refetchOnWindowFocus: false,
    // Name resolution is purely a display nicety - silently falling back to
    // the URL is better than retrying a 404 or a reconnect error.
    retry: false,
  });
}
