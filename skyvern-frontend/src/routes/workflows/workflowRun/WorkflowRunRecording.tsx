import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import { artifactApiBaseUrl } from "@/util/env";

function WorkflowRunRecording() {
  const { data: workflowRun } = useWorkflowRunQuery();
  let recordingURL = workflowRun?.recording_url;
  if (recordingURL?.startsWith("file://")) {
    recordingURL = `${artifactApiBaseUrl}/artifact/recording?path=${recordingURL.slice(7)}`;
  }
  return recordingURL ? (
    <video src={recordingURL} controls className="w-full rounded-md" />
  ) : (
    <div>No recording available for this workflow</div>
  );
}

export { WorkflowRunRecording };
