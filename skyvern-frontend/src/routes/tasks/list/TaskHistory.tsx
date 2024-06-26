import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { PAGE_SIZE } from "../constants";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { TaskListSkeletonRows } from "./TaskListSkeletonRows";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { StatusBadge } from "@/components/StatusBadge";
import { basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { TaskActions } from "./TaskActions";

function TaskHistory() {
  const credentialGetter = useCredentialGetter();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const navigate = useNavigate();

  const {
    data: tasks,
    isPending,
    isError,
    error,
  } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", "history", page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(PAGE_SIZE));
      params.append("task_status", "completed");
      params.append("task_status", "failed");
      params.append("task_status", "terminated");
      params.append("task_status", "timed_out");
      params.append("task_status", "canceled");

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

  function handleNavigate(event: React.MouseEvent, id: string) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + `/tasks/${id}/actions`,
        "_blank",
        "noopener,noreferrer",
      );
    } else {
      navigate(`${id}/actions`);
    }
  }

  return (
    <>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-1/4">ID</TableHead>
              <TableHead className="w-1/4">URL</TableHead>
              <TableHead className="w-1/6">Status</TableHead>
              <TableHead className="w-1/4">Created At</TableHead>
              <TableHead className="w-1/12" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {isPending ? (
              <TaskListSkeletonRows />
            ) : tasks?.length === 0 ? (
              <TableRow>
                <TableCell colSpan={3}>No tasks found</TableCell>
              </TableRow>
            ) : (
              tasks?.map((task) => {
                return (
                  <TableRow key={task.task_id}>
                    <TableCell
                      className="w-1/4 cursor-pointer"
                      onClick={(event) => handleNavigate(event, task.task_id)}
                    >
                      {task.task_id}
                    </TableCell>
                    <TableCell
                      className="w-1/4 cursor-pointer max-w-64 overflow-hidden whitespace-nowrap overflow-ellipsis"
                      onClick={(event) => handleNavigate(event, task.task_id)}
                    >
                      {task.request.url}
                    </TableCell>
                    <TableCell
                      className="w-1/6 cursor-pointer"
                      onClick={(event) => handleNavigate(event, task.task_id)}
                    >
                      <StatusBadge status={task.status} />
                    </TableCell>
                    <TableCell
                      className="w-1/4 cursor-pointer"
                      onClick={(event) => handleNavigate(event, task.task_id)}
                    >
                      {basicTimeFormat(task.created_at)}
                    </TableCell>
                    <TableCell className="w-1/12">
                      <TaskActions task={task} />
                    </TableCell>
                  </TableRow>
                );
              })
            )}
          </TableBody>
        </Table>
      </div>

      <Pagination className="pt-2">
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
  );
}

export { TaskHistory };
