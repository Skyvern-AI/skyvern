import { getClient } from "@/api/AxiosClient";
import { ArtifactVideo } from "@/components/ArtifactVideo";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { getRecordingURL } from "./artifactUtils";
import { Skeleton } from "@/components/ui/skeleton";
import { TaskApiResponse } from "@/api/types";
import { useFirstParam } from "@/hooks/useFirstParam";

function TaskRecording() {
  const taskId = useFirstParam("taskId", "runId");
  const credentialGetter = useCredentialGetter();

  const {
    data: recordingData,
    isLoading: taskIsLoading,
    isError: taskIsError,
  } = useQuery<{ url: string | null; archived: boolean }>({
    queryKey: ["task", taskId, "recordingURL"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const task: TaskApiResponse = await client
        .get(`/tasks/${taskId}`)
        .then((response) => response.data);
      return {
        url: getRecordingURL(task),
        archived: task.recording_archived ?? false,
      };
    },
    refetchOnMount: true,
  });

  if (taskIsLoading) {
    return (
      <div className="h-[450px] w-[800px]">
        <Skeleton className="h-full" />
      </div>
    );
  }

  if (taskIsError) {
    return <div>Error loading recording</div>;
  }

  if (recordingData?.url) {
    return (
      <ArtifactVideo
        width={800}
        height={450}
        src={recordingData.url}
        controls
      />
    );
  }

  if (recordingData?.archived) {
    return (
      <div className="text-muted-foreground">
        This recording has been archived. To request restoration, please contact
        support@skyvern.com
        {/* TODO: add a "Request Restore" button */}
      </div>
    );
  }

  return <div>No recording available for this task</div>;
}

export { TaskRecording };
