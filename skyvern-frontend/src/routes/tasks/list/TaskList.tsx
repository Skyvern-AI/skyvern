import { client } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { useQuery } from "@tanstack/react-query";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { useNavigate, useSearchParams } from "react-router-dom";
import { TaskListSkeleton } from "./TaskListSkeleton";
import { RunningTasks } from "../running/RunningTasks";
import { cn } from "@/util/utils";
import { PAGE_SIZE } from "../constants";
import { StatusBadge } from "@/components/StatusBadge";
import { basicTimeFormat } from "@/util/timeFormat";
import { QueuedTasks } from "../running/QueuedTasks";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

function TaskList() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;

  const {
    data: tasks,
    isPending,
    isError,
    error,
  } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", "all", page],
    queryFn: async () => {
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(PAGE_SIZE));
      params.append("task_status", "completed");
      params.append("task_status", "failed");
      params.append("task_status", "terminated");
      return client
        .get("/tasks", {
          params,
        })
        .then((response) => response.data);
    },
  });

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  const resolvedTasks = tasks?.filter(
    (task) =>
      task.status === "completed" ||
      task.status === "failed" ||
      task.status === "terminated",
  );

  return (
    <div className="flex flex-col gap-8 max-w-5xl mx-auto">
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Running Tasks</CardTitle>
          <CardDescription>Tasks that are currently running</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <div className="grid grid-cols-4 gap-4">
            <RunningTasks />
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Queued Tasks</CardTitle>
          <CardDescription>Tasks that are waiting to run</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          <QueuedTasks />
        </CardContent>
      </Card>
      <Card>
        <CardHeader className="border-b-2">
          <CardTitle className="text-xl">Task History</CardTitle>
          <CardDescription>Tasks you have run previously</CardDescription>
        </CardHeader>
        <CardContent className="p-4">
          {isPending ? (
            <TaskListSkeleton />
          ) : (
            <>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead className="w-1/3">URL</TableHead>
                    <TableHead className="w-1/3">Status</TableHead>
                    <TableHead className="w-1/3">Created At</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tasks.length === 0 ? (
                    <TableRow>
                      <TableCell colSpan={3}>No tasks found</TableCell>
                    </TableRow>
                  ) : (
                    resolvedTasks?.map((task) => {
                      return (
                        <TableRow
                          key={task.task_id}
                          className="cursor-pointer w-4"
                          onClick={() => {
                            navigate(task.task_id);
                          }}
                        >
                          <TableCell className="w-1/3">
                            {task.request.url}
                          </TableCell>
                          <TableCell className="w-1/3">
                            <StatusBadge status={task.status} />
                          </TableCell>
                          <TableCell className="w-1/3">
                            {basicTimeFormat(task.created_at)}
                          </TableCell>
                        </TableRow>
                      );
                    })
                  )}
                </TableBody>
              </Table>
              <Pagination>
                <PaginationContent>
                  <PaginationItem>
                    <PaginationPrevious
                      href="#"
                      className={cn({ "cursor-not-allowed": page === 1 })}
                      onClick={() => {
                        if (page === 1) {
                          return;
                        }
                        const params = new URLSearchParams();
                        params.set("page", String(Math.max(1, page - 1)));
                        setSearchParams(params);
                      }}
                    />
                  </PaginationItem>
                  <PaginationItem>
                    <PaginationLink href="#">{page}</PaginationLink>
                  </PaginationItem>
                  <PaginationItem>
                    <PaginationNext
                      href="#"
                      onClick={() => {
                        const params = new URLSearchParams();
                        params.set("page", String(page + 1));
                        setSearchParams(params);
                      }}
                    />
                  </PaginationItem>
                </PaginationContent>
              </Pagination>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export { TaskList };
