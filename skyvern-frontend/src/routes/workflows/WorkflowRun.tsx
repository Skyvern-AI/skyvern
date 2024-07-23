import { getClient } from "@/api/AxiosClient";
import { TaskApiResponse, WorkflowRunStatusApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { TaskListSkeletonRows } from "../tasks/list/TaskListSkeletonRows";
import { basicTimeFormat } from "@/util/timeFormat";
import { TaskActions } from "../tasks/list/TaskActions";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";

function WorkflowRun() {
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useQuery<WorkflowRunStatusApiResponse>({
      queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
      queryFn: async () => {
        const client = await getClient(credentialGetter);
        return client
          .get(`/workflows/${workflowPermanentId}/runs/${workflowRunId}`)
          .then((response) => response.data);
      },
    });

  const { data: workflowTasks, isLoading: workflowTasksIsLoading } = useQuery<
    Array<TaskApiResponse>
  >({
    queryKey: ["workflowTasks", workflowRunId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get(`/tasks?workflow_run_id=${workflowRunId}&page_size=200`)
        .then((response) => response.data);
    },
  });

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

  const parameters = workflowRun?.parameters ?? {};

  return (
    <div className="space-y-8">
      <header className="flex gap-2">
        <h1 className="text-lg font-semibold">{workflowRunId}</h1>
        {workflowRunIsLoading ? (
          <Skeleton className="h-8 w-28" />
        ) : workflowRun ? (
          <StatusBadge status={workflowRun?.status} />
        ) : null}
      </header>
      <div className="space-y-4">
        <header>
          <h2 className="text-lg font-semibold">Tasks</h2>
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
              {workflowTasksIsLoading ? (
                <TaskListSkeletonRows />
              ) : workflowTasks?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5}>
                    This workflow run does not have any tasks
                  </TableCell>
                </TableRow>
              ) : (
                workflowTasks?.map((task) => {
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
      </div>
      <div className="space-y-4">
        <header>
          <h2 className="text-lg font-semibold">Parameters</h2>
        </header>
        {Object.entries(parameters).map(([key, value]) => {
          return (
            <div key={key} className="flex flex-col gap-2">
              <Label>{key}</Label>
              {typeof value === "string" ? (
                <Input value={value} readOnly />
              ) : (
                <Input value={JSON.stringify(value)} readOnly />
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export { WorkflowRun };
