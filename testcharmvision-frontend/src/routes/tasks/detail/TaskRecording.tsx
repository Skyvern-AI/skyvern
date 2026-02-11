import { getClient } from "@/api/AxiosClient";
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
    data: recordingURL,
    isLoading: taskIsLoading,
    isError: taskIsError,
  } = useQuery<string | null>({
    queryKey: ["task", taskId, "recordingURL"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const task: TaskApiResponse = await client
        .get(`/tasks/${taskId}`)
        .then((response) => response.data);
      return getRecordingURL(task);
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

  return recordingURL ? (
    <video width={800} height={450} src={recordingURL} controls />
  ) : (
    <div>No recording available for this task</div>
  );
}

export { TaskRecording };
