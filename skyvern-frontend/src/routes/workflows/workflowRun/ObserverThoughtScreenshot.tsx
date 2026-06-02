import { useEffect, useState } from "react";
import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, ArtifactType, Status } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  SCREENSHOT_PANEL_CLASS,
  StreamStatusPanel,
} from "@/routes/streaming/StreamDiagnostics";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { getImageURL } from "@/routes/tasks/detail/artifactUtils";
import { apiPathPrefix } from "@/util/env";

type Props = {
  observerThoughtId: string;
  taskStatus?: Status; // to give a hint that screenshot may not be available if task is not finalized
};

function ObserverThoughtScreenshot({ observerThoughtId, taskStatus }: Props) {
  const credentialGetter = useCredentialGetter();
  const [imageFailed, setImageFailed] = useState(false);

  useEffect(() => {
    setImageFailed(false);
  }, [observerThoughtId]);

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
      <StreamStatusPanel
        className={SCREENSHOT_PANEL_CLASS}
        diagnostic={{
          title: "Looking for the screenshot",
          detail: "Just a sec while we fetch it.",
          pending: true,
        }}
      />
    );
  }

  if (
    !screenshot &&
    taskStatus &&
    statusIsNotFinalized({ status: taskStatus })
  ) {
    return (
      <StreamStatusPanel
        className={SCREENSHOT_PANEL_CLASS}
        diagnostic={{
          title: "Still capturing this screenshot",
          detail:
            "The agent's working on it — checking back every few seconds.",
          pending: true,
        }}
      />
    );
  }

  if (!screenshot) {
    return (
      <StreamStatusPanel
        className={SCREENSHOT_PANEL_CLASS}
        diagnostic={{
          title: "No screenshot for this one",
          detail: "The agent didn't capture one here.",
        }}
      />
    );
  }

  if (imageFailed) {
    return (
      <StreamStatusPanel
        className={SCREENSHOT_PANEL_CLASS}
        diagnostic={{
          title: "This screenshot got away from us",
          detail: "The artifact's there, but the image wouldn't load.",
          hint: "Refresh the page or open the artifact directly.",
        }}
      />
    );
  }

  return (
    <figure className="mx-auto flex max-w-full flex-col items-center gap-2 overflow-hidden rounded">
      <ZoomableImage
        src={getImageURL(screenshot)}
        alt="llm-screenshot"
        onError={() => setImageFailed(true)}
      />
    </figure>
  );
}

export { ObserverThoughtScreenshot };
