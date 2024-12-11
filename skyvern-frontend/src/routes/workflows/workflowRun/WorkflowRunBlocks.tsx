import { getClient } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import {
  keepPreviousData,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import { useWorkflowRunQuery } from "../hooks/useWorkflowRunQuery";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { WorkflowBlockCollapsibleContent } from "../WorkflowBlockCollapsibleContent";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import { cn } from "@/util/utils";
import {
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "@/routes/tasks/types";
import { useEffect, useState } from "react";
import { envCredential } from "@/util/env";
import { toast } from "@/components/ui/use-toast";
import { ZoomableImage } from "@/components/ZoomableImage";
import { AspectRatio } from "@/components/ui/aspect-ratio";
import { Label } from "@/components/ui/label";
import { StatusBadge } from "@/components/StatusBadge";
import {
  localTimeFormatWithShortDate,
  timeFormatWithShortDate,
} from "@/util/timeFormat";
import { Button } from "@/components/ui/button";
import { ReaderIcon } from "@radix-ui/react-icons";

type StreamMessage = {
  task_id: string;
  status: string;
  screenshot?: string;
};

let socket: WebSocket | null = null;

const wssBaseUrl = import.meta.env.VITE_WSS_BASE_URL;

function WorkflowRunBlocks() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [streamImgSrc, setStreamImgSrc] = useState<string>("");
  const { workflowRunId, workflowPermanentId } = useParams();
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const { data: workflowRun, isLoading: workflowRunIsLoading } =
    useWorkflowRunQuery();

  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);

  const showStream = workflowRun && statusIsNotFinalized(workflowRun);

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
            queryClient.invalidateQueries({
              queryKey: ["workflowRuns"],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
            });
            queryClient.invalidateQueries({
              queryKey: ["workflowTasks", workflowRunId],
            });
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
  }, [
    credentialGetter,
    workflowRunId,
    workflowRunIsRunningOrQueued,
    queryClient,
    workflowPermanentId,
  ]);

  function getStream() {
    if (workflowRun?.status === Status.Created) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-900 py-8 text-lg">
          <span>Workflow has been created.</span>
          <span>Stream will start when the workflow is running.</span>
        </div>
      );
    }
    if (workflowRun?.status === Status.Queued) {
      return (
        <div className="flex h-full w-full flex-col items-center justify-center gap-8 rounded-md bg-slate-900 py-8 text-lg">
          <span>Your workflow run is queued.</span>
          <span>Stream will start when the workflow is running.</span>
        </div>
      );
    }

    if (workflowRun?.status === Status.Running && streamImgSrc.length === 0) {
      return (
        <div className="flex h-full w-full items-center justify-center rounded-md bg-slate-900 py-8 text-lg">
          Starting the stream...
        </div>
      );
    }

    if (workflowRun?.status === Status.Running && streamImgSrc.length > 0) {
      return (
        <div className="h-full w-full">
          <ZoomableImage
            src={`data:image/png;base64,${streamImgSrc}`}
            className="rounded-md"
          />
        </div>
      );
    }
    return null;
  }

  const { data: workflowTasks, isLoading: workflowTasksIsLoading } = useQuery<
    Array<TaskApiResponse>
  >({
    queryKey: ["workflowTasks", workflowRunId, page],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", "20");
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
    refetchOnMount: workflowRun?.status === Status.Running ? "always" : false,
    refetchOnWindowFocus:
      workflowRun?.status === Status.Running ? "always" : false,
  });

  const skeleton = (
    <TableRow>
      <TableCell className="w-10">
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell className="w-1/5">
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell className="w-1/6">
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell className="w-1/4">
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell className="w-1/8">
        <Skeleton className="h-6 w-full" />
      </TableCell>
      <TableCell className="w-1/5">
        <Skeleton className="h-6 w-full" />
      </TableCell>
    </TableRow>
  );

  const currentRunningTask = workflowTasks?.find(
    (task) => task.status === Status.Running,
  );

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

  return (
    <div className="space-y-8">
      {showStream && (
        <div className="space-y-4">
          <header>
            <h1 className="text-2xl">Live Stream</h1>
          </header>
          <div className="flex gap-5">
            <div className="w-3/4 shrink-0">
              <AspectRatio ratio={16 / 9}>{getStream()}</AspectRatio>
            </div>
            <div className="flex w-full min-w-0 flex-col gap-4 rounded-md bg-slate-elevation1 p-4">
              <header className="text-lg">Current Task</header>
              {workflowRunIsLoading || !currentRunningTask ? (
                <div>Waiting for a task to start...</div>
              ) : (
                <div className="flex h-full flex-col gap-2">
                  <div className="flex gap-2 rounded-sm bg-slate-elevation3 p-2">
                    <Label className="text-sm text-slate-400">ID</Label>
                    <span className="text-sm">
                      {currentRunningTask.task_id}
                    </span>
                  </div>
                  <div className="flex gap-2 rounded-sm bg-slate-elevation3 p-2">
                    <Label className="text-sm text-slate-400">URL</Label>
                    <span
                      className="truncate text-sm"
                      title={currentRunningTask.request.url}
                    >
                      {currentRunningTask.request.url}
                    </span>
                  </div>
                  <div className="flex items-center gap-2 rounded-sm bg-slate-elevation3 p-2">
                    <Label className="text-sm text-slate-400">Status</Label>
                    <span className="text-sm">
                      <StatusBadge status={currentRunningTask.status} />
                    </span>
                  </div>
                  <div className="flex gap-2 rounded-sm bg-slate-elevation3 p-2">
                    <Label className="text-sm text-slate-400">Created</Label>
                    <span
                      className="truncate text-sm"
                      title={timeFormatWithShortDate(
                        currentRunningTask.created_at,
                      )}
                    >
                      {currentRunningTask &&
                        localTimeFormatWithShortDate(
                          currentRunningTask.created_at,
                        )}
                    </span>
                  </div>
                  <div className="mt-auto flex justify-end">
                    <Button asChild>
                      <Link to={`/tasks/${currentRunningTask.task_id}/actions`}>
                        <ReaderIcon className="mr-2 h-4 w-4" />
                        View Actions
                      </Link>
                    </Button>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      )}
      <div className="space-y-4">
        <header>
          <h1 className="text-2xl">Workflow Blocks</h1>
        </header>
        <div className="rounded-md border">
          <Table>
            <TableHeader className="rounded-t-md bg-slate-elevation2">
              <TableRow>
                <TableHead className="w-10 rounded-tl-md"></TableHead>
                <TableHead className="w-1/5 text-slate-400">
                  Task Title
                </TableHead>
                <TableHead className="w-1/6 text-slate-400">ID</TableHead>
                <TableHead className="w-1/4 text-slate-400">URL</TableHead>
                <TableHead className="w-1/8 text-slate-400">Status</TableHead>
                <TableHead className="w-1/5 rounded-tr-md text-slate-400">
                  Created At
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {workflowTasksIsLoading ? (
                skeleton
              ) : workflowTasks?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={6}>Could not find any tasks</TableCell>
                </TableRow>
              ) : (
                workflowTasks
                  ?.filter(
                    (task) => task.task_id !== currentRunningTask?.task_id,
                  )
                  .map((task) => {
                    return (
                      <WorkflowBlockCollapsibleContent
                        key={task.task_id}
                        task={task}
                        onNavigate={handleNavigate}
                      />
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
    </div>
  );
}

export { WorkflowRunBlocks };
