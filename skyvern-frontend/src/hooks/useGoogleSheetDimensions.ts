import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { GetSheetDimensionsResponse } from "@/api/types";
import {
  extractSpreadsheetIdFromUrl,
  isTemplateExpression,
} from "@/util/googleSheetsUrl";

type Options = {
  credentialId: string;
  spreadsheetUrlOrId: string;
  sheetName: string;
};

export function useGoogleSheetDimensions({
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

  return useQuery<GetSheetDimensionsResponse>({
    queryKey: [
      "googleSheets",
      "dimensions",
      credentialId,
      spreadsheetId,
      sheetName,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get(
        `/google/sheets/spreadsheets/${spreadsheetId}/dimensions`,
        {
          params: {
            credential_id: credentialId,
            sheet_title: sheetName,
          },
        },
      );
      return response.data as GetSheetDimensionsResponse;
    },
    enabled,
    staleTime: 30_000,
  });
}
