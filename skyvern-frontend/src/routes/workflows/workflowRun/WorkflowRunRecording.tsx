import { usePostHog } from "posthog-js/react";
import { useWorkflowRunWithWorkflowQuery } from "../hooks/useWorkflowRunWithWorkflowQuery";
import { artifactApiBaseUrl } from "@/util/env";

function resolveUrl(url: string): string {
  if (url.startsWith("file://")) {
    return `${artifactApiBaseUrl}/artifact/recording?path=${url.slice(7)}`;
  }
  return url;
}

function WorkflowRunRecording() {
  const postHog = usePostHog();
  const { data: workflowRun } = useWorkflowRunWithWorkflowQuery();

  const rawUrls =
    workflowRun?.recording_urls && workflowRun.recording_urls.length > 0
      ? workflowRun.recording_urls
      : workflowRun?.recording_url
        ? [workflowRun.recording_url]
        : [];
  const recordingUrls = rawUrls.map(resolveUrl);

  if (!workflowRun || recordingUrls.length === 0) {
    return <div>No recording available for this workflow</div>;
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

  if (recordingUrls.length === 1) {
    return (
      <video
        src={recordingUrls[0]}
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
          <div className="text-sm text-slate-400">
            Recording {index + 1} of {recordingUrls.length}
          </div>
          <video
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
