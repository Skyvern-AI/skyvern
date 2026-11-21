import { queryOptions, useQuery } from "@tanstack/react-query";
import { useCallback } from "react";
import { getClient, type CredentialGetter } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useFeatureFlag } from "@/hooks/useFeatureFlag";
import {
  type ActiveOrgId,
  getActiveOrgQueryKeyScope,
  getOrgScopedQueryKey,
  useActiveOrgId,
} from "@/store/ActiveOrgContext";

export type IntegrationCatalogEntry = {
  id: string;
  status: "live" | "comingSoon";
};

const INTEGRATION_CATALOG_QUERY_KEY = ["integrationCatalog"] as const;

// The catalog route lists these for every org; they show only where the org's DRIVE_FOLDER_ACTIONS flag is on.
const GATED_DRIVE_ACTION_IDS = new Set([
  "list_folder",
  "create_folder",
  "move_file",
]);

export function withoutGatedActions<T extends IntegrationCatalogEntry>(
  catalog: T[],
  driveFolderActions: boolean,
): T[] {
  if (driveFolderActions) return catalog;
  return catalog.map((entry) => {
    const actions = (entry as { actions?: Array<{ id: string }> }).actions;
    if (entry.id !== "google_drive" || !Array.isArray(actions)) return entry;
    return {
      ...entry,
      actions: actions.filter(
        (action) => !GATED_DRIVE_ACTION_IDS.has(action.id),
      ),
    };
  });
}

export function integrationCatalogQueryOptions<
  T extends IntegrationCatalogEntry,
>(credentialGetter: CredentialGetter | null, activeOrgId: ActiveOrgId) {
  return queryOptions<T[]>({
    queryKey: getOrgScopedQueryKey(
      INTEGRATION_CATALOG_QUERY_KEY,
      getActiveOrgQueryKeyScope(activeOrgId),
    ),
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<T[]>("/integrations");
      return response.data;
    },
  });
}

export function useIntegrationCatalogQuery<
  T extends IntegrationCatalogEntry,
>() {
  const credentialGetter = useCredentialGetter();
  const activeOrgId = useActiveOrgId();
  const driveFolderActions = useFeatureFlag("DRIVE_FOLDER_ACTIONS") === true;
  const select = useCallback(
    (catalog: T[]) => withoutGatedActions(catalog, driveFolderActions),
    [driveFolderActions],
  );
  return useQuery({
    ...integrationCatalogQueryOptions<T>(credentialGetter, activeOrgId),
    select,
  });
}
