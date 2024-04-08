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
import { basicTimeFormat } from "@/util/timeFormat";
import { LatestScreenshot } from "./LatestScreenshot";

function RunningTasks() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;

  const { data: tasks } = useQuery<Array<TaskApiResponse>>({
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

  const runningTasks = tasks?.filter((task) => task.status === Status.Running);

  if (runningTasks?.length === 0) {
    return <div>No running tasks</div>;
  }

  return runningTasks?.map((task) => {
    return (
      <Card
        key={task.task_id}
        className="hover:bg-primary-foreground cursor-pointer"
        onClick={() => {
          navigate(`/tasks/${task.task_id}`);
        }}
      >
        <CardHeader>
          <CardTitle>{task.task_id}</CardTitle>
          <CardDescription className="whitespace-nowrap overflow-hidden text-ellipsis">
            {task.request.url}
          </CardDescription>
        </CardHeader>
        <CardContent>
          Latest screenshot:
          <div className="w-40 h-40 border-2">
            <LatestScreenshot id={task.task_id} />
          </div>
        </CardContent>
        <CardFooter>Created: {basicTimeFormat(task.created_at)}</CardFooter>
      </Card>
    );
  });
}

export { RunningTasks };
