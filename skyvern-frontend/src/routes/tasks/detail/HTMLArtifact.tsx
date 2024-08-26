import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQuery } from "@tanstack/react-query";
import axios from "axios";

// https://stackoverflow.com/a/60338028
function format(html: string) {
  const tab = "\t";
  let result = "";
  let indent = "";

  html.split(/>\s*</).forEach(function (element) {
    if (element.match(/^\/\w/)) {
      indent = indent.substring(tab.length);
    }

    result += indent + "<" + element + ">\r\n";

    if (element.match(/^<?\w[^>]*[^/]$/) && !element.startsWith("input")) {
      indent += tab;
    }
  });

  return result.substring(1, result.length - 3);
}

type Props = {
  artifact: ArtifactApiResponse;
};

function HTMLArtifact({ artifact }: Props) {
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
    return <Skeleton className="h-48 w-full" />;
  }

  return (
    <Textarea
      className="w-full"
      rows={15}
      value={isError ? JSON.stringify(error) : format(data ?? "")}
      readOnly
    />
  );
}

export { HTMLArtifact };
