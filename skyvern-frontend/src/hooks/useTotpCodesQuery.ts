import { getClient } from "@/api/AxiosClient";
import type { TotpCode, TotpCodeListParams } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { isAxiosError } from "axios";
import { useMemo } from "react";
import { useQuery, type UseQueryOptions } from "@tanstack/react-query";

type QueryFnData = TotpCode[];
type QueryKey = ["totpCodes", TotpCodeListParams];

type Options = {
  params: TotpCodeListParams;
  enabled?: boolean;
  queryOptions?: Omit<
    UseQueryOptions<QueryFnData, unknown, QueryFnData, QueryKey>,
    "queryKey" | "queryFn"
  >;
};

type UseTotpCodesQueryReturn = ReturnType<
  typeof useQuery<QueryFnData, unknown, QueryFnData, QueryKey>
> & {
  isFeatureUnavailable: boolean;
  isCredentialAccountMissing: boolean;
};

function useTotpCodesQuery({
  params,
  enabled = true,
  queryOptions,
}: Options): UseTotpCodesQueryReturn {
  const credentialGetter = useCredentialGetter();

  const searchParams = useMemo(() => {
    const result = new URLSearchParams();
    if (params.totp_identifier) {
      result.set("totp_identifier", params.totp_identifier);
    }
    if (params.workflow_run_id) {
      result.set("workflow_run_id", params.workflow_run_id);
    }
    if (params.otp_type) {
      result.set("otp_type", params.otp_type);
    }
    if (typeof params.limit === "number") {
      result.set("limit", String(params.limit));
    }
    return result;
  }, [params]);

  const query = useQuery<QueryFnData, unknown, QueryFnData, QueryKey>({
    queryKey: ["totpCodes", params],
    enabled,
    queryFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      const response = await client.get<QueryFnData>("/credentials/totp", {
        params: searchParams,
      });
      return response.data;
    },
    retry(failureCount, error) {
      if (isAxiosError(error) && error.response?.status === 404) {
        return false;
      }
      return failureCount < 3;
    },
    ...queryOptions,
  });

  const axiosError = isAxiosError(query.error) ? query.error : null;
  const errorStatus = axiosError?.response?.status;
  const errorDetail =
    (axiosError?.response?.data as { detail?: string } | undefined)?.detail ??
    "";

  const isCredentialAccountMissing =
    errorStatus === 404 && errorDetail.includes("Credential account not found");

  const isFeatureUnavailable =
    errorStatus === 404 && !isCredentialAccountMissing;

  return {
    ...query,
    isFeatureUnavailable,
    isCredentialAccountMissing,
  };
}

export { useTotpCodesQuery };
