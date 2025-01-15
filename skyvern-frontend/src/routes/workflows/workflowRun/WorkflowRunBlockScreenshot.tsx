import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ReloadIcon } from "@radix-ui/react-icons";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { apiPathPrefix } from "@/util/env";

type Props = {
  workflowRunBlockId: string;
};

function WorkflowRunBlockScreenshot({ workflowRunBlockId }: Props) {
  const credentialGetter = useCredentialGetter();

  const { data: artifacts, isLoading } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["workflowRunBlock", workflowRunBlockId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(
          `${apiPathPrefix}/workflow_run_block/${workflowRunBlockId}/artifacts`,
        )
        .then((response) => response.data);
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      const screenshot = data?.filter(
        (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
      )?.[0];
      if (!screenshot) {
        return 5000;
      }
      return false;
    },
  });

  const llmScreenshots = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
  );

  const screenshot = llmScreenshots?.[0];

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 bg-slate-elevation1">
        <ReloadIcon className="h-6 w-6 animate-spin" />
        <div>Loading screenshot...</div>
      </div>
    );
  }

  if (!screenshot) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-elevation1">
        No screenshot found for this workflow run block.
      </div>
    );
  }

  return (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden rounded">
      <ZoomableImage src={getImageURL(screenshot)} alt="llm-screenshot" />
    </figure>
  );
}

export { WorkflowRunBlockScreenshot };
