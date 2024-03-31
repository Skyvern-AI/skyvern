import { client } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { PAGE_SIZE } from "../constants";
import { RunningTaskSkeleton } from "./RunningTaskSkeleton";

function RunningTasks() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;

  const {
    data: tasks,
    isPending,
    isError,
    error,
  } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", page],
    queryFn: async () => {
      return client
        .get("/tasks", {
          params: {
            page,
            page_size: PAGE_SIZE,
          },
        })
        .then((response) => response.data);
    },
    refetchInterval: 3000,
    placeholderData: keepPreviousData,
  });

  if (isPending) {
    return <RunningTaskSkeleton />;
  }

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  if (!tasks) {
    return null;
  }

  const runningTasks = tasks.filter((task) => task.status === Status.Running);

  if (runningTasks.length === 0) {
    return <div>No running tasks</div>;
  }

  return runningTasks.map((task) => {
    return (
      <Card
        key={task.task_id}
        className="hover:bg-primary-foreground cursor-pointer"
        onClick={() => {
          navigate(`/tasks/${task.task_id}`);
        }}
      >
        <CardHeader>
          <CardTitle>{task.request.url}</CardTitle>
          <CardDescription></CardDescription>
        </CardHeader>
        <CardContent>Goal: {task.request.navigation_goal}</CardContent>
        <CardFooter>Created: {basicTimeFormat(task.created_at)}</CardFooter>
      </Card>
    );
  });
}

export { RunningTasks };
