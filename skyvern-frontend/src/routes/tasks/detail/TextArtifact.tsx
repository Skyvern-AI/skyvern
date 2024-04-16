import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";

type Props = {
  artifact: ArtifactApiResponse;
};

function TextArtifact({ artifact }: Props) {
  const { data, isFetching, isError, error } = useQuery<string>({
    queryKey: ["artifact", artifact.artifact_id],
    queryFn: async () => {
      if (artifact.uri.startsWith("file://")) {
        return artifactApiClient
          .get(`/artifact/text`, {
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
    return <Skeleton className="w-full h-48" />;
  }

  return (
    <Textarea
      className="w-full"
      rows={15}
      value={isError ? JSON.stringify(error) : data}
      readOnly
    />
  );
}

export { TextArtifact };
