import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { GoogleSheetTab, ListGoogleSheetTabsResponse } from "@/api/types";
import { extractSpreadsheetIdFromUrl } from "@/util/googleSheetsUrl";

type Options = {
  credentialId: string;
  spreadsheetUrlOrId: string;
  enabled: boolean;
};

export function useGoogleSheetTabs({
  credentialId,
  spreadsheetUrlOrId,
  enabled,
}: Options) {
  const credentialGetter = useCredentialGetter();
  const spreadsheetId = extractSpreadsheetIdFromUrl(spreadsheetUrlOrId);

  return useQuery<GoogleSheetTab[]>({
    queryKey: ["googleSheets", "tabs", credentialId, spreadsheetId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get(
        `/google/sheets/spreadsheets/${spreadsheetId}/tabs`,
        { params: { credential_id: credentialId } },
      );
      return (response.data as ListGoogleSheetTabsResponse).tabs;
    },
    enabled: enabled && Boolean(credentialId) && Boolean(spreadsheetId),
    staleTime: 30_000,
  });
}
