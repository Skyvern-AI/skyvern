import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { GetSheetHeadersResponse, SheetHeader } from "@/api/types";
import {
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";

type Options = {
  credentialId: string;
  spreadsheetUrlOrId: string;
  sheetName: string;
};

export function useGoogleSheetHeaders({
  credentialId,
  spreadsheetUrlOrId,
  sheetName,
}: Options) {
  const credentialGetter = useCredentialGetter();
  const spreadsheetId = extractSpreadsheetIdFromUrl(spreadsheetUrlOrId);
  const urlIsTemplated = isTemplateExpression(spreadsheetUrlOrId);
  const nameIsTemplated = isTemplateExpression(sheetName);
  const credentialIsTemplated = isTemplateExpression(credentialId);
  const enabled =
    Boolean(credentialId) &&
    Boolean(spreadsheetId) &&
    Boolean(sheetName) &&
    !urlIsTemplated &&
    !nameIsTemplated &&
    !credentialIsTemplated;

  return useQuery<SheetHeader[]>({
    queryKey: [
      "googleSheets",
      "headers",
      credentialId,
      spreadsheetId,
      sheetName,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get(
        `/google/sheets/spreadsheets/${spreadsheetId}/headers`,
        {
          params: {
            credential_id: credentialId,
            sheet_title: sheetName,
          },
        },
      );
      return (response.data as GetSheetHeadersResponse).headers;
    },
    enabled,
    staleTime: 30_000,
  });
}
