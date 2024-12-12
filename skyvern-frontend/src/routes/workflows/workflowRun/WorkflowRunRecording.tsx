import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";

function WorkflowRunRecording() {
  const { data: workflowRun } = useWorkflowRunQuery();
  const recordingURL = workflowRun?.recording_url;
  return recordingURL ? (
    <video src={recordingURL} controls className="w-full rounded-md" />
  ) : (
    <div>No recording available for this workflow</div>
  );
}

export { WorkflowRunRecording };
