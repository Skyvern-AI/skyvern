import { client } from "@/api/AxiosClient";
import {
  ArtifactApiResponse,
  ArtifactType,
  StepApiResponse,
} from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { artifactApiBaseUrl } from "@/util/env";
import { useQuery } from "@tanstack/react-query";

type Props = {
  id: string;
};

function LatestScreenshot({ id }: Props) {
  const {
    data: screenshotUri,
    isFetching,
    isError,
  } = useQuery<string | undefined>({
    queryKey: ["task", id, "latestScreenshot"],
    queryFn: async () => {
      const steps: StepApiResponse[] = await client
        .get(`/tasks/${id}/steps`)
        .then((response) => response.data);

      if (steps.length === 0) {
        return;
      }

      const latestStep = steps[steps.length - 1];

      if (!latestStep) {
        return;
      }

      const artifacts: ArtifactApiResponse[] = await client
        .get(`/tasks/${id}/steps/${latestStep.step_id}/artifacts`)
        .then((response) => response.data);

      const actionScreenshotUris = artifacts
        ?.filter(
          (artifact) =>
            artifact.artifact_type === ArtifactType.ActionScreenshot,
        )
        .map((artifact) => artifact.uri);

      if (actionScreenshotUris.length > 0) {
        return actionScreenshotUris[0];
      }

      const llmScreenshotUris = artifacts
        ?.filter(
          (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
        )
        .map((artifact) => artifact.uri);

      if (llmScreenshotUris.length > 0) {
        return llmScreenshotUris[0];
      }

      return Promise.reject("No screenshots found");
    },
    refetchInterval: 2000,
  });

  if (isFetching) {
    return <Skeleton className="w-full h-full" />;
  }

  if (isError || !screenshotUri || typeof screenshotUri !== "string") {
    return null;
  }

  return (
    <img
      src={`${artifactApiBaseUrl}/artifact/image?path=${screenshotUri.slice(7)}`}
      className="w-full h-full object-contain"
      alt="Latest screenshot"
    />
  );
}

export { LatestScreenshot };
