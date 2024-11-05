import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType, Status } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getImageURL } from "./artifactUtils";
import { ReloadIcon } from "@radix-ui/react-icons";
import { statusIsNotFinalized } from "../types";

type Props = {
  stepId: string;
  index: number;
  taskStatus?: Status; // to give a hint that screenshot may not be available if task is not finalized
};

function ActionScreenshot({ stepId, index, taskStatus }: Props) {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();

  const { data: artifacts, isLoading } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["task", taskId, "steps", stepId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks/${taskId}/steps/${stepId}/artifacts`)
        .then((response) => response.data);
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      const screenshot = data?.filter(
        (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
      )?.[index];
      if (!screenshot) {
        return 5000;
      }
      return false;
    },
  });

  const actionScreenshots = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
  );

  const screenshot = actionScreenshots?.[index];

  if (isLoading) {
    return (
      <div className="mx-auto flex max-h-[400px] flex-col items-center gap-2 overflow-hidden">
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
    return <div>No screenshot found for this action.</div>;
  }

  return (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden">
      <ZoomableImage src={getImageURL(screenshot)} alt="llm-screenshot" />
    </figure>
  );
}

export { ActionScreenshot };
