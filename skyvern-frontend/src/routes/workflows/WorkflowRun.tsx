import { getClient } from "@/api/AxiosClient";
import {
  Status,
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
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
import { basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { TaskActions } from "../tasks/list/TaskActions";
import { TaskListSkeletonRows } from "../tasks/list/TaskListSkeletonRows";
import { useEffect, useState } from "react";
import { statusIsNotFinalized, statusIsRunningOrQueued } from "../tasks/types";
import { envCredential } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import { Pencil2Icon, PlayIcon } from "@radix-ui/react-icons";

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
};

let socket: WebSocket | null = null;

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRun() {
  const [searchParams, setSearchParams] = useSearchParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const { workflowRunId, workflowPermanentId } = useParams();
  const credentialGetter = useCredentialGetter();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
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
      refetchInterval: (query) => {
        if (!query.state.data) {
          return false;
        }
        if (statusIsNotFinalized(query.state.data)) {
          return 5000;
        }
        return false;
      },
      placeholderData: keepPreviousData,
    });

  const { data: workflowTasks, isLoading: workflowTasksIsLoading } = useQuery<
    Array<TaskApiResponse>
  >({
    queryKey: ["workflowTasks", workflowRunId, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      return client
        .get(`/tasks?workflow_run_id=${workflowRunId}`, { params })
        .then((response) => response.data);
    },
    refetchInterval: () => {
      if (workflowRun?.status === Status.Running) {
        return 5000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
    refetchOnMount: workflowRun?.status === Status.Running,
  });

  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);

  useEffect(() => {
    if (!workflowRunIsRunningOrQueued) {
      return;
    }

    async function run() {
      // Create WebSocket connection.
      let credential = null;
      if (credentialGetter) {
        const token = await credentialGetter();
        credential = `?token=Bearer ${token}`;
      } else {
        credential = `?apikey=${envCredential}`;
      }
      if (socket) {
        socket.close();
      }
      socket = new WebSocket(
        `${wssBaseUrl}/stream/workflow_runs/${workflowRunId}${credential}`,
      );
      // Listen for messages
      socket.addEventListener("message", (event) => {
        try {
          const message: StreamMessage = JSON.parse(event.data);
          if (message.screenshot) {
            setStreamImgSrc(message.screenshot);
          }
          if (
            message.status === "completed" ||
            message.status === "failed" ||
            message.status === "terminated"
          ) {
            socket?.close();
            if (
              message.status === "failed" ||
              message.status === "terminated"
            ) {
              toast({
                title: "Run Failed",
                description: "The workflow run has failed.",
                variant: "destructive",
              });
            } else if (message.status === "completed") {
              toast({
                title: "Run Completed",
                description: "The workflow run has been completed.",
                variant: "success",
              });
            }
          }
        } catch (e) {
          console.error("Failed to parse message", e);
        }
      });

      socket.addEventListener("close", () => {
        socket = null;
      });
    }
    run();

    return () => {
      if (socket) {
        socket.close();
        socket = null;
      }
    };
  }, [credentialGetter, workflowRunId, workflowRunIsRunningOrQueued]);

  function getStream() {
    if (workflowRun?.status === Status.Created) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-8 bg-slate-900 py-8 text-lg">
          <span>Workflow has been created.</span>
          <span>Stream will start when the workflow is running.</span>
        </div>
      );
    }
    if (workflowRun?.status === Status.Queued) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-8 bg-slate-900 py-8 text-lg">
          <span>Your workflow run is queued.</span>
          <span>Stream will start when the workflow is running.</span>
        </div>
      );
    }

    if (workflowRun?.status === Status.Running && streamImgSrc.length === 0) {
      return (
        <div className="flex h-full w-full items-center justify-center bg-slate-900 py-8 text-lg">
          Starting the stream...
        </div>
      );
    }

    if (workflowRun?.status === Status.Running && streamImgSrc.length > 0) {
      return (
        <div className="h-full w-full">
          <img src={`data:image/png;base64,${streamImgSrc}`} />
        </div>
      );
    }
    return null;
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

  const parameters = workflowRun?.parameters ?? {};

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="flex gap-2">
          <h1 className="text-lg font-semibold">{workflowRunId}</h1>
          {workflowRunIsLoading ? (
            <Skeleton className="h-8 w-28" />
          ) : workflowRun ? (
            <StatusBadge status={workflowRun?.status} />
          ) : null}
        </div>
        <div className="flex gap-2">
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/edit`}>
              <Pencil2Icon className="mr-2 h-4 w-4" />
              Edit Workflow
            </Link>
          </Button>
          <Button asChild>
            <Link
              to={`/workflows/${workflowPermanentId}/run`}
              state={{
                data: parameters,
              }}
            >
              <PlayIcon className="mr-2 h-4 w-4" />
              Rerun Workflow
            </Link>
          </Button>
        </div>
      </header>
      {getStream()}
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
                  <TableCell colSpan={5}>No tasks</TableCell>
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
