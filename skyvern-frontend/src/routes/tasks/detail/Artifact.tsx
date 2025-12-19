import { artifactApiClient } from "@/api/AxiosClient";
import { ArtifactApiResponse } from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
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

function getFormattedResult(type: "json" | "html" | "text", result: unknown) {
  switch (type) {
    case "json":
      return JSON.stringify(result, null, 2);
    case "html":
      return format(result as string);
    case "text":
      return result;
  }
}

function getEndpoint(type: "json" | "html" | "text") {
  switch (type) {
    case "json":
      return "/artifact/json";
    case "html":
    case "text":
      return "/artifact/text";
  }
}

type Props = {
  type: "json" | "html" | "text";
  artifacts: Array<ArtifactApiResponse>;
};

function Artifact({ type, artifacts }: Props) {
  function fetchArtifact(artifact: ArtifactApiResponse) {
    if (artifact.signed_url) {
      return axios.get(artifact.signed_url).then((response) => response.data);
    }
    if (artifact.uri.startsWith("file://")) {
      const endpoint = getEndpoint(type);
      return artifactApiClient
        .get(endpoint, {
          params: {
            path: artifact.uri.slice(7),
          },
        })
        .then((response) => response.data);
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
    <CodeEditor
      language={type === "text" ? undefined : type}
      className="w-full"
      value={
        results.some((result) => result.isError)
          ? JSON.stringify(results.find((result) => result.isError)?.error)
          : results
              .map((result) => getFormattedResult(type, result.data))
              .join(",\n")
      }
      minHeight="96px"
      maxHeight="500px"
      readOnly
    />
  );
}

export { Artifact };
