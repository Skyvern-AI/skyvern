import { useQuery } from "@tanstack/react-query";
import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import type { OrganizationScheduleListResponse } from "@/routes/workflows/types/scheduleTypes";

function useOrganizationSchedulesQuery(opts: {
  page: number;
  pageSize: number;
  statusFilter?: string;
  search?: string;
}) {
  const credentialGetter = useCredentialGetter();

  const params = new URLSearchParams();
  params.set("page", String(opts.page));
  params.set("page_size", String(opts.pageSize));
  if (opts.statusFilter) {
    params.set("status", opts.statusFilter);
  }
  if (opts.search) {
    params.set("search", opts.search);
  }

  return useQuery<OrganizationScheduleListResponse>({
    queryKey: [
      "organizationSchedules",
      opts.page,
      opts.pageSize,
      opts.statusFilter,
      opts.search,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const response = await client.get<OrganizationScheduleListResponse>(
        `/schedules?${params.toString()}`,
      );
      return response.data;
    },
    staleTime: 30_000,
  });
}

export { useOrganizationSchedulesQuery };
