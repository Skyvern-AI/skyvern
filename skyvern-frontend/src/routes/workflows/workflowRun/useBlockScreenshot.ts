import { useQuery } from "@tanstack/react-query";

import { getClient } from "@/api/AxiosClient";
import { type ArtifactApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { apiPathPrefix } from "@/util/env";

import { selectBlockScreenshot } from "./blockScreenshot";

// Shares the artifact query key with the screenshot panes, so a failure card asking whether a
// capture exists rides their cache instead of issuing a second request.
function useBlockScreenshot(
  workflowRunBlockId: string | undefined,
  blockType: string | null | undefined,
  enabled: boolean,
): ArtifactApiResponse | undefined {
  const credentialGetter = useCredentialGetter();
  const { data } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["workflowRunBlock", workflowRunBlockId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(
          `${apiPathPrefix}/workflow_run_block/${workflowRunBlockId}/artifacts`,
        )
        .then((response) => response.data);
    },
    enabled: enabled && Boolean(workflowRunBlockId),
    refetchOnWindowFocus: false,
    staleTime: Infinity,
  });
  return selectBlockScreenshot(data, blockType ?? undefined);
}

export { useBlockScreenshot };
