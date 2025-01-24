import { getClient } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { StatusFilterDropdown } from "@/components/StatusFilterDropdown";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { TaskActions } from "./TaskActions";
import { TaskListSkeletonRows } from "./TaskListSkeletonRows";
import { Button } from "@/components/ui/button";
import { DownloadIcon } from "@radix-ui/react-icons";
import { downloadBlob } from "@/util/downloadBlob";

function TaskHistory() {
  const credentialGetter = useCredentialGetter();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const navigate = useNavigate();
  const [statusFilters, setStatusFilters] = useState<Array<Status>>([]);

  const {
    data: tasks,
    isPending,
    isError,
    error,
  } = useQuery<Array<TaskApiResponse>>({
    queryKey: ["tasks", { statusFilters }, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);

      const params = new URLSearchParams();
      params.append("page", String(page));
      statusFilters.forEach((status) => {
        params.append("task_status", status);
      });
      params.append("only_standalone_tasks", "true");

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

  function handleExport() {
    if (!tasks) {
      return; // should never happen
    }
    const data = ["id,url,status,created,failure_reason"];
    tasks.forEach((task) => {
      const row = [
        task.task_id,
        task.request.url,
        task.status,
        task.created_at,
        task.failure_reason ?? "",
      ];
      data.push(
        row
          .map(String) // convert every value to String
          .map((v) => v.replace(new RegExp('"', "g"), '""')) // escape double quotes
          .map((v) => `"${v}"`) // quote it
          .join(","), // comma-separated
      );
    });
    const contents = data.join("\r\n");

    downloadBlob(contents, "export.csv", "data:text/csv;charset=utf-8;");
  }

  return (
    <div className="space-y-4">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl">Task Runs</h1>
        <div className="flex gap-2">
          <StatusFilterDropdown
            values={statusFilters}
            onChange={setStatusFilters}
          />
          <Button variant="secondary" onClick={handleExport}>
            <DownloadIcon className="mr-2" />
            Export CSV
          </Button>
        </div>
      </header>
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
                <TableCell colSpan={5}>No tasks found</TableCell>
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
                      className="w-1/4 max-w-64 cursor-pointer overflow-hidden overflow-ellipsis whitespace-nowrap"
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
                      title={basicTimeFormat(task.created_at)}
                    >
                      {basicLocalTimeFormat(task.created_at)}
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
        <Pagination className="pt-2">
          <PaginationContent>
            <PaginationItem>
              <PaginationPrevious
                className={cn({ "cursor-not-allowed": page === 1 })}
                onClick={() => {
                  if (page === 1) {
                    return;
                  }
                  const params = new URLSearchParams();
                  params.set("page", String(Math.max(1, page - 1)));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
            <PaginationItem>
              <PaginationLink>{page}</PaginationLink>
            </PaginationItem>
            <PaginationItem>
              <PaginationNext
                onClick={() => {
                  const params = new URLSearchParams();
                  params.set("page", String(page + 1));
                  setSearchParams(params, { replace: true });
                }}
              />
            </PaginationItem>
          </PaginationContent>
        </Pagination>
      </div>
    </div>
  );
}

export { TaskHistory };
