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
import { Link, useSearchParams } from "react-router-dom";
import { TaskListSkeleton } from "./TaskListSkeleton";
import { Button } from "@/components/ui/button";
import { PlusIcon } from "@radix-ui/react-icons";

function TaskList() {
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
        .get("/internal/tasks", {
          params: {
            page,
          },
        })
        .then((response) => response.data);
    },
  });

  if (isPending) {
    return <TaskListSkeleton />;
  }

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex justify-between">
        <h1>Tasks</h1>
        <Button asChild>
          <Link to="new">
            <PlusIcon className="mr-2" /> New Task
          </Link>
        </Button>
      </div>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>URL</TableHead>
            <TableHead>Status</TableHead>
            <TableHead>Created At</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {tasks.length === 0 ? (
            <TableRow>
              <TableCell colSpan={3}>No tasks found</TableCell>
            </TableRow>
          ) : (
            tasks.map((task) => {
              const date = new Date(task.created_at);
              const dateString = date.toLocaleDateString("en-us", {
                weekday: "long",
                year: "numeric",
                month: "short",
                day: "numeric",
              });
              const timeString = date.toLocaleTimeString("en-us");

              return (
                <TableRow
                  key={task.task_id}
                  className="cursor-pointer w-4"
                  onClick={() => console.log(task)}
                >
                  <TableCell>{task.url}</TableCell>
                  <TableCell>{task.status}</TableCell>
                  <TableCell>
                    {dateString} at {timeString}
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
              onClick={() => {
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
