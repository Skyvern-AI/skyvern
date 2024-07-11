import { getClient } from "@/api/AxiosClient";
import {
  ArtifactApiResponse,
  ArtifactType,
  StepApiResponse,
} from "@/api/types";
import { Skeleton } from "@/components/ui/skeleton";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { getImageURL } from "../detail/artifactUtils";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

type Props = {
  id: string;
};

function LatestScreenshot({ id }: Props) {
  const credentialGetter = useCredentialGetter();

  const {
    data: artifact,
    isFetching,
    isError,
  } = useQuery<ArtifactApiResponse | undefined>({
    queryKey: ["task", id, "latestScreenshot"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
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

      const actionScreenshots = artifacts?.filter(
        (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
      );

      if (actionScreenshots.length > 0) {
        return actionScreenshots[0];
      }

      const llmScreenshots = artifacts?.filter(
        (artifact) => artifact.artifact_type === ArtifactType.LLMScreenshot,
      );

      if (llmScreenshots.length > 0) {
        return llmScreenshots[0];
      }

      return Promise.reject("No screenshots found");
    },
    refetchInterval: 10000,
    placeholderData: keepPreviousData,
  });

  if (isFetching && !artifact) {
    return <Skeleton className="h-full w-full" />;
  }

  if (isError || !artifact) {
    return null;
  }

  return (
    <img
      src={getImageURL(artifact)}
      className="h-full w-full object-contain"
      alt="Latest screenshot"
    />
  );
}

export { LatestScreenshot };
