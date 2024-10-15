import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { useQueries } from "@tanstack/react-query";
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
  artifacts: Array<ArtifactApiResponse>;
};

function HTMLArtifact({ artifacts }: Props) {
  function fetchArtifact(artifact: ArtifactApiResponse) {
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
    <Textarea
      className="w-full"
      rows={15}
      value={
        results.some((result) => result.isError)
          ? JSON.stringify(results.find((result) => result.isError)?.error)
          : results.map((result) => format(result.data ?? "")).join(",\n")
      }
      readOnly
    />
  );
}

export { HTMLArtifact };
