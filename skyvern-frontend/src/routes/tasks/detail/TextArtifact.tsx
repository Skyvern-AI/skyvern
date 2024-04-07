import { artifactApiClient } from "@/api/AxiosClient";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQuery } from "@tanstack/react-query";

type Props = {
  uri: string;
};

function TextArtifact({ uri }: Props) {
  const { data, isFetching, isError, error } = useQuery<string>({
    queryKey: ["artifact", uri],
    queryFn: async () => {
      return artifactApiClient
        .get(`/artifact/text`, {
          params: {
            path: uri.slice(7),
          },
        })
        .then((response) => response.data);
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
