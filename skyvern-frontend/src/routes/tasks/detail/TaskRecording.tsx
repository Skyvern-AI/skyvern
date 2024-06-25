import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { getRecordingURL } from "./artifactUtils";
import { useParams } from "react-router-dom";
import { Skeleton } from "@/components/ui/skeleton";

function TaskRecording() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();

  const {
    data: task,
    isFetching: taskIsFetching,
    isError: taskIsError,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
  });

  if (taskIsFetching) {
    return (
      <div className="flex mx-auto">
        <div className="w-[800px] h-[450px]">
          <Skeleton className="h-full" />
        </div>
      </div>
    );
  }

  if (taskIsError || !task) {
    return <div>Error loading recording</div>;
  }

  return (
    <div className="flex mx-auto">
      {task.recording_url ? (
        <video width={800} height={450} src={getRecordingURL(task)} controls />
      ) : (
        <div>No recording available</div>
      )}
    </div>
  );
}

export { TaskRecording };
