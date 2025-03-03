import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType, Status } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { ReloadIcon } from "@radix-ui/react-icons";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { apiPathPrefix } from "@/util/env";

type Props = {
  observerThoughtId: string;
  taskStatus?: Status; // to give a hint that screenshot may not be available if task is not finalized
};

function ObserverThoughtScreenshot({ observerThoughtId, taskStatus }: Props) {
  const credentialGetter = useCredentialGetter();

  const { data: artifacts, isLoading } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["observerThought", observerThoughtId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/thought/${observerThoughtId}/artifacts`)
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

  // use the last screenshot as the llmScreenshots are in reverse order
  const screenshot = llmScreenshots?.[llmScreenshots.length - 1];

  if (isLoading) {
    return (
      <div className="flex h-full items-center justify-center gap-2 bg-slate-elevation1">
        <ReloadIcon className="h-6 w-6 animate-spin" />
        <div>Loading screenshot...</div>
      </div>
    );
  }

  if (
    !screenshot &&
    taskStatus &&
    statusIsNotFinalized({ status: taskStatus })
  ) {
    return <div>The screenshot for this action is not available yet.</div>;
  }

  if (!screenshot) {
    return (
      <div className="flex h-full items-center justify-center bg-slate-elevation1">
        No screenshot found for this thought.
      </div>
    );
  }

  return (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden rounded">
      <ZoomableImage src={getImageURL(screenshot)} alt="llm-screenshot" />
    </figure>
  );
}

export { ObserverThoughtScreenshot };
