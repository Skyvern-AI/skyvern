import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType, Status } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { getImageURL } from "./artifactUtils";
import { ReloadIcon } from "@radix-ui/react-icons";
import { statusIsNotFinalized } from "../types";
import { apiPathPrefix } from "@/util/env";

type Props = {
  stepId: string;
  index: number;
  taskStatus?: Status; // to give a hint that screenshot may not be available if task is not finalized
};

function ActionScreenshot({ stepId, index, taskStatus }: Props) {
  const credentialGetter = useCredentialGetter();

  const {
    data: artifacts,
    isLoading,
    isFetching,
  } = useQuery<Array<ArtifactApiResponse>>({
    queryKey: ["step", stepId, "artifacts"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`${apiPathPrefix}/step/${stepId}/artifacts`)
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

  // action screenshots are reverse ordered w.r.t action order
  const screenshot = actionScreenshots?.[actionScreenshots.length - index - 1];

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
  } else if (isFetching) {
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
        No screenshot found for this action.
      </div>
    );
  }

  return (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden rounded">
      <ZoomableImage src={getImageURL(screenshot)} alt="llm-screenshot" />
    </figure>
  );
}

export { ActionScreenshot };
