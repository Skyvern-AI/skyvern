import { getClient } from "@/api/AxiosClient";
import {
  Status,
  TaskApiResponse,
  WorkflowRunStatusApiResponse,
} from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import { ZoomableImage } from "@/components/ZoomableImage";
import { AspectRatio } from "@/components/ui/aspect-ratio";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
import { toast } from "@/components/ui/use-toast";
import { useApiCredential } from "@/hooks/useApiCredential";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { copyText } from "@/util/copyText";
import { apiBaseUrl, envCredential } from "@/util/env";
import {
  localTimeFormatWithShortDate,
  timeFormatWithShortDate,
} from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  CopyIcon,
  Pencil2Icon,
  PlayIcon,
  ReaderIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import fetchToCurl from "fetch-to-curl";
import { useEffect, useState } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
import {
  statusIsFinalized,
  statusIsNotFinalized,
  statusIsRunningOrQueued,
} from "../tasks/types";
import { WorkflowBlockCollapsibleContent } from "./WorkflowBlockCollapsibleContent";
import { CodeEditor } from "./components/CodeEditor";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";

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
  const apiCredential = useApiCredential();
  const queryClient = useQueryClient();

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

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
      refetchOnMount: (query) => {
        if (!query.state.data) {
          return false;
        }
        return statusIsRunningOrQueued(query.state.data) ? "always" : false;
      },
      refetchOnWindowFocus: (query) => {
        if (!query.state.data) {
          return false;
        }
        return statusIsRunningOrQueued(query.state.data);
      },
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
    refetchOnMount: workflowRun?.status === Status.Running ? "always" : false,
    refetchOnWindowFocus:
      workflowRun?.status === Status.Running ? "always" : false,
  });

  const cancelWorkflowMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/workflows/runs/${workflowRunId}/cancel`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowRunId],
      });
      queryClient.invalidateQueries({
        queryKey: ["workflowRun", workflowPermanentId, workflowRunId],
      });
      toast({
        variant: "success",
        title: "Workflow Canceled",
        description: "The workflow has been successfully canceled.",
      });
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "Error",
        description: error.message,
      });
    },
  });

  const currentRunningTask = workflowTasks?.find(
    (task) => task.status === Status.Running,
  );

  const workflowRunIsRunningOrQueued =
    workflowRun && statusIsRunningOrQueued(workflowRun);

  const workflowRunIsFinalized = workflowRun && statusIsFinalized(workflowRun);

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

  const title = workflowIsLoading ? (
    <Skeleton className="h-9 w-48" />
  ) : (
    <h1 className="text-3xl">{workflow?.title}</h1>
  );

  const workflowFailureReason = workflowRun?.failure_reason ? (
    <div
      className="space-y-2 rounded-md border border-red-600 p-4"
      style={{
        backgroundColor: "rgba(220, 38, 38, 0.10)",
      }}
    >
      <div className="font-bold">Workflow Failure Reason</div>
      <div className="text-sm">{workflowRun.failure_reason}</div>
    </div>
  ) : null;

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

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="space-y-3">
          <div className="flex items-center gap-5">
            {title}
            {workflowRunIsLoading ? (
              <Skeleton className="h-8 w-28" />
            ) : workflowRun ? (
              <StatusBadge status={workflowRun?.status} />
            ) : null}
          </div>
          <h2 className="text-2xl text-slate-400">{workflowRunId}</h2>
        </div>

        <div className="flex gap-2">
          <Button
            variant="secondary"
            onClick={() => {
              if (!workflowRun) {
                return;
              }
              const curl = fetchToCurl({
                method: "POST",
                url: `${apiBaseUrl}/workflows/${workflowPermanentId}/run`,
                body: {
                  data: workflowRun?.parameters,
                  proxy_location: "RESIDENTIAL",
                },
                headers: {
                  "Content-Type": "application/json",
                  "x-api-key": apiCredential ?? "<your-api-key>",
                },
              });
              copyText(curl).then(() => {
                toast({
                  variant: "success",
                  title: "Copied to Clipboard",
                  description:
                    "The cURL command has been copied to your clipboard.",
                });
              });
            }}
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            cURL
          </Button>
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/edit`}>
              <Pencil2Icon className="mr-2 h-4 w-4" />
              Edit
            </Link>
          </Button>
          {workflowRunIsRunningOrQueued && (
            <Dialog>
              <DialogTrigger asChild>
                <Button variant="destructive">Cancel</Button>
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>Are you sure?</DialogTitle>
                  <DialogDescription>
                    Are you sure you want to cancel this workflow run?
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <DialogClose asChild>
                    <Button variant="secondary">Back</Button>
                  </DialogClose>
                  <Button
                    variant="destructive"
                    onClick={() => {
                      cancelWorkflowMutation.mutate();
                    }}
                    disabled={cancelWorkflowMutation.isPending}
                  >
                    {cancelWorkflowMutation.isPending && (
                      <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                    )}
                    Cancel Workflow Run
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
          )}
          {workflowRunIsFinalized && (
            <Button asChild>
              <Link
                to={`/workflows/${workflowPermanentId}/run`}
                state={{
                  data: parameters,
                }}
              >
                <PlayIcon className="mr-2 h-4 w-4" />
                Rerun
              </Link>
            </Button>
          )}
        </div>
      </header>
      {workflowFailureReason}
      {showStream && (
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
                  <span className="text-sm">{currentRunningTask.task_id}</span>
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
      )}
      <div className="space-y-5">
        <header>
          <h2 className="text-2xl">
            {workflowRunIsRunningOrQueued ? "Previous Blocks" : "Blocks"}
          </h2>
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
      {workflowRunIsFinalized && (
        <div className="space-y-4">
          <header>
            <h2 className="text-lg font-semibold">Block Outputs</h2>
          </header>
          <CodeEditor
            language="json"
            value={JSON.stringify(workflowRun.outputs, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="500px"
          />
        </div>
      )}
      {Object.entries(parameters).length > 0 && (
        <div className="space-y-4">
          <header>
            <h2 className="text-lg font-semibold">Input Parameter Values</h2>
          </header>
          {Object.entries(parameters).length === 0 && (
            <div>This workflow doesn't have any input parameters.</div>
          )}
          {Object.entries(parameters).map(([key, value]) => {
            return (
              <div key={key} className="flex flex-col gap-2">
                <Label>{key}</Label>
                {typeof value === "string" ||
                typeof value === "number" ||
                typeof value === "boolean" ? (
                  <Input value={String(value)} readOnly />
                ) : (
                  <CodeEditor
                    value={JSON.stringify(value, null, 2)}
                    readOnly
                    language="json"
                    minHeight="96px"
                    maxHeight="500px"
                  />
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export { WorkflowRun };
