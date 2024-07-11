import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";

type Props = {
  artifact: ArtifactApiResponse;
};

function JSONArtifact({ artifact }: Props) {
  const { data, isFetching, isError, error } = useQuery<
    Record<string, unknown>
  >({
    queryKey: ["artifact", artifact.artifact_id],
    queryFn: async () => {
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
    },
  });

  if (isFetching) {
    return <Skeleton className="h-48 w-full" />;
  }

  return (
    <Textarea
      className="w-full"
      rows={15}
      value={isError ? JSON.stringify(error) : JSON.stringify(data, null, 2)}
      readOnly
    />
  );
}

export { JSONArtifact };
