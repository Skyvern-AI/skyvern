import { useQueryClient } from "@tanstack/react-query";
import { OrganizationApiResponse } from "@/api/types";

// Cache-only read of the current org id. The cloud shell (RootLayout +
// useApiCredential) populates ["organizations"] on entry; this hook returns
// undefined when the cache is empty, which keeps the OSS bundle from firing
// any extra GET /organizations/ requests at render time. Returns the first
// (primary) org — same convention used by RootLayout, useApiCredential, and
// CloudSettings; multi-org users still emit a stable per-account org_id.
export function useCurrentOrgId(): string | undefined {
  const queryClient = useQueryClient();
  const data = queryClient.getQueryData<Array<OrganizationApiResponse>>([
    "organizations",
  ]);
  return data?.[0]?.organization_id;
}
