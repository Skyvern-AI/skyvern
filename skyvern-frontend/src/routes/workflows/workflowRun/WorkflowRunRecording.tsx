import { usePostHog } from "posthog-js/react";
import { ArtifactVideo } from "@/components/ArtifactVideo";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { getRecordingUrls } from "./recordingUrls";

function WorkflowRunRecording() {
  const postHog = usePostHog();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();

  const recordingUrls = getRecordingUrls(workflowRun);

  if (!workflowRun || recordingUrls.length === 0) {
    if (workflowRun?.recording_archived) {
      return (
        <div className="text-muted-foreground">
          This recording has been archived. To request restoration, please
          contact support@skyvern.com
          {/* TODO: add a "Request Restore" button */}
        </div>
      );
    }
    return <div>No recording available for this agent</div>;
  }

  const run = workflowRun;

  function handlePlay(index: number) {
    postHog.capture("run.recording.viewed", {
      org_id: run.workflow.organization_id,
      run_id: run.workflow_run_id,
      recording_index: index,
      recording_count: recordingUrls.length,
    });
  }

  const singleUrl = recordingUrls.length === 1 ? recordingUrls[0] : undefined;
  if (singleUrl) {
    return (
      <ArtifactVideo
        src={singleUrl}
        controls
        preload="metadata"
        className="w-full rounded-md"
        onPlay={() => handlePlay(0)}
      />
    );
  }

  return (
    <div className="flex flex-col gap-4">
      {recordingUrls.map((url, index) => (
        <div
          key={index} // presigned URLs change on refetch; list order is stable
          className="flex flex-col gap-2"
        >
          <div className="text-sm text-muted-foreground">
            Recording {index + 1} of {recordingUrls.length}
          </div>
          <ArtifactVideo
            src={url}
            controls
            preload="metadata"
            className="w-full rounded-md"
            onPlay={() => handlePlay(index)}
          />
        </div>
      ))}
    </div>
  );
}

export { WorkflowRunRecording };
