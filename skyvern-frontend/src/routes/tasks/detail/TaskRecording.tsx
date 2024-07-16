import { getClient } from "@/api/AxiosClient";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { getRecordingURL } from "./artifactUtils";
import { useParams } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";

function TaskRecording() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();

  const {
    data: recordingURL,
    isLoading: taskIsLoading,
    isError: taskIsError,
  } = useQuery<string | undefined>({
    queryKey: ["task", taskId, "recordingURL"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const task = await client
        .get(`/tasks/${taskId}`)
        .then((response) => response.data);
      return getRecordingURL(task);
    },
    refetchOnMount: true,
  });

  if (taskIsLoading) {
    return (
      <div className="mx-auto flex">
        <div className="h-[450px] w-[800px]">
          <Skeleton className="h-full" />
        </div>
      </div>
    );
  }

  if (taskIsError) {
    return <div>Error loading recording</div>;
  }

  return (
    <div className="mx-auto flex">
      {recordingURL ? (
        <video width={800} height={450} src={recordingURL} controls />
      ) : (
        <div>No recording available</div>
      )}
    </div>
  );
}

export { TaskRecording };
