import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { LatestScreenshot } from "./LatestScreenshot";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";

function RunningTasks() {
  const navigate = useNavigate();
  const credentialGetter = useCredentialGetter();

  const { data: runningTasks } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", "running"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get("/tasks", {
          params: {
            task_status: "running",
            only_standalone_tasks: "true",
          },
        })
        .then((response) => response.data);
    },
    refetchOnMount: "always",
  });

  if (runningTasks?.length === 0) {
    return <div>No running tasks</div>;
  }

  function handleNavigate(event: React.MouseEvent, id: string) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + `/tasks/${id}/actions`,
        "_blank",
        "noopener,noreferrer",
      );
    } else {
      navigate(`/tasks/${id}/actions`);
    }
  }

  return runningTasks?.map((task) => {
    return (
      <Card
        key={task.task_id}
        className="cursor-pointer hover:bg-muted/50"
        onClick={(event) => handleNavigate(event, task.task_id)}
      >
        <CardHeader>
          <CardTitle className="overflow-hidden text-ellipsis whitespace-nowrap">
            {task.task_id}
          </CardTitle>
          <CardDescription className="overflow-hidden text-ellipsis whitespace-nowrap">
            {task.request.url}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex items-center justify-center">
          <div className="h-40 w-40">
            <LatestScreenshot id={task.task_id} />
          </div>
        </CardContent>
        <CardFooter title={basicTimeFormat(task.created_at)}>
          Created: {basicLocalTimeFormat(task.created_at)}
        </CardFooter>
      </Card>
    );
  });
}

export { RunningTasks };
