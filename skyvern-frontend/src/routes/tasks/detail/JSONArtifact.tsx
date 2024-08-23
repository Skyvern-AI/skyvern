import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQueries } from "@tanstack/react-query";
import axios from "axios";

type Props = {
  artifacts: Array<ArtifactApiResponse>;
};

function JSONArtifact({ artifacts }: Props) {
  function fetchArtifact(artifact: ArtifactApiResponse) {
    if (artifact.uri.startsWith("file://")) {
      return artifactApiClient
        .get(`/artifact/json`, {
          params: {
            path: artifact.uri.slice(7),
          },
        })
        .then((response) => response.data);
    }
    if (artifact.uri.startsWith("s3://") && artifact.signed_url) {
      return axios.get(artifact.signed_url).then((response) => response.data);
    }
  }

  const results = useQueries({
    queries:
      artifacts?.map((artifact) => {
        return {
          queryKey: ["artifact", artifact.artifact_id],
          queryFn: () => fetchArtifact(artifact),
        };
      }) ?? [],
  });

  if (results.some((result) => result.isLoading)) {
    return <Skeleton className="h-48 w-full" />;
  }

  return (
    <>
      <Textarea
        className="w-full"
        rows={15}
        value={
          results.some((result) => result.isError)
            ? JSON.stringify(results.find((result) => result.isError)?.error)
            : results
                .map((result) => JSON.stringify(result.data, null, 2))
                .join(",\n")
        }
        readOnly
      />
    </>
  );
}

export { JSONArtifact };
