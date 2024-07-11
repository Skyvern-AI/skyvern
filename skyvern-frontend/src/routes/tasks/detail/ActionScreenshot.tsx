import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useParams } from "react-router-dom";
import { getImageURL } from "./artifactUtils";
import { ReloadIcon } from "@radix-ui/react-icons";

type Props = {
  stepId: string;
  index: number;
};

function ActionScreenshot({ stepId, index }: Props) {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();

  const { data: artifacts, isFetching } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["task", taskId, "steps", stepId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks/${taskId}/steps/${stepId}/artifacts`)
        .then((response) => response.data);
    },
  });

  const actionScreenshots = artifacts?.filter(
    (artifact) => artifact.artifact_type === ArtifactType.ActionScreenshot,
  );

  const screenshot = actionScreenshots?.[index];

  if (isFetching) {
    return (
      <div className="mx-auto flex max-h-[400px] flex-col items-center gap-2 overflow-hidden">
        <ReloadIcon className="h-6 w-6 animate-spin" />
        <div>Loading screenshot...</div>
      </div>
    );
  }

  return screenshot ? (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden">
      <ZoomableImage src={getImageURL(screenshot)} alt="llm-screenshot" />
    </figure>
  ) : (
    <div>Screenshot not found</div>
  );
}

export { ActionScreenshot };
