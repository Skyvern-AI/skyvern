import { getClient } from "@/api/AxiosClient";
import { ArtifactApiResponse, Status } from "@/api/types";
import { ZoomableImage } from "@/components/ZoomableImage";
import { useArtifactImageSrc } from "@/hooks/useArtifactImageSrc";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import {
  SCREENSHOT_PANEL_CLASS,
  StreamStatusPanel,
} from "@/routes/streaming/StreamDiagnostics";
import { statusIsNotFinalized } from "@/routes/tasks/types";
import { apiPathPrefix } from "@/util/env";
import { isBlockScreenshot, selectBlockScreenshot } from "./blockScreenshot";

type Props = {
  workflowRunBlockId: string;
  blockType?: string;
  runStatus?: Status;
};

function WorkflowRunBlockScreenshot({
  workflowRunBlockId,
  blockType,
  runStatus,
}: Props) {
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
      // While the run is active, keep polling so the latest screenshot shows (a code block's
      // action screenshots land after its pre-execution LLM screenshot). Once finalized, stop:
      // no further screenshots are captured, including for promptless code blocks that never
      // produce one. Status unknown -> poll until any screenshot appears.
      if (runStatus !== undefined) {
        return statusIsNotFinalized({ status: runStatus }) ? 5000 : false;
      }
      return query.state.data?.some(isBlockScreenshot) ? false : 5000;
    },
  });

  const screenshot = selectBlockScreenshot(artifacts, blockType);
  const { src, onImageError, imageFailed } = useArtifactImageSrc(screenshot);

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

  const runIsActive =
    runStatus !== undefined && statusIsNotFinalized({ status: runStatus });

  if (!screenshot && runIsActive) {
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
          title: "No screenshot for this block",
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
      <ZoomableImage src={src} alt="block screenshot" onError={onImageError} />
    </figure>
  );
}

export { WorkflowRunBlockScreenshot };
