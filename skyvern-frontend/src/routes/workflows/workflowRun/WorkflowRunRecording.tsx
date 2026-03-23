import { usePostHog } from "posthog-js/react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { artifactApiBaseUrl } from "@/util/env";

function WorkflowRunRecording() {
  const postHog = usePostHog();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();
  let recordingURL = workflowRun?.recording_url;
  if (recordingURL?.startsWith("file://")) {
    recordingURL = `${artifactApiBaseUrl}/artifact/recording?path=${recordingURL.slice(7)}`;
  }

  function handlePlay() {
    if (!workflowRun) {
      return;
    }
    postHog.capture("run.recording.viewed", {
      org_id: workflowRun.workflow.organization_id,
      run_id: workflowRun.workflow_run_id,
    });
  }

  return recordingURL ? (
    <video
      src={recordingURL}
      controls
      className="w-full rounded-md"
      onPlay={handlePlay}
    />
  ) : (
    <div>No recording available for this workflow</div>
  );
}

export { WorkflowRunRecording };
