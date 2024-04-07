import { client } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
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
    return <TaskListSkeleton />;
  }

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  if (!tasks) {
    return null;
  }

  const resolvedTasks = tasks.filter(
    (task) =>
      task.status === "completed" ||
      task.status === "failed" ||
      task.status === "terminated",
  );

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-2xl py-2 border-b-2">Running Tasks</h1>
      <div className="grid grid-cols-4 gap-4">
        <RunningTasks />
      </div>
      <h1 className="text-2xl py-2 border-b-2">Task History</h1>
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
            resolvedTasks.map((task) => {
              return (
                <TableRow
                  key={task.task_id}
                  className="cursor-pointer w-4"
                  onClick={() => {
                    navigate(task.task_id);
                  }}
                >
                  <TableCell className="w-1/3">{task.request.url}</TableCell>
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
    </div>
  );
}

export { TaskList };
