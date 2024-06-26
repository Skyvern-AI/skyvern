import { getClient } from "@/api/AxiosClient";
import { Status, TaskApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
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
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { cn } from "@/util/utils";
import { ReloadIcon } from "@radix-ui/react-icons";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { NavLink, Outlet, useParams } from "react-router-dom";

function TaskDetails() {
  const { taskId } = useParams();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const {
    data: task,
    isLoading: taskIsLoading,
    isError: taskIsError,
    error: taskError,
  } = useQuery<TaskApiResponse>({
    queryKey: ["task", taskId],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client.get(`/tasks/${taskId}`).then((response) => response.data);
    },
    refetchInterval: (query) => {
      if (
        query.state.data?.status === Status.Running ||
        query.state.data?.status === Status.Queued
      ) {
        return 10000;
      }
      return false;
    },
    placeholderData: keepPreviousData,
  });

  const cancelTaskMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .post(`/tasks/${taskId}/cancel`)
        .then((response) => response.data);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["task", taskId],
      });
      queryClient.invalidateQueries({
        queryKey: ["tasks"],
      });
      toast({
        variant: "success",
        title: "Task Canceled",
        description: "The task has been successfully canceled.",
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

  if (taskIsError) {
    return <div>Error: {taskError?.message}</div>;
  }

  const showExtractedInformation =
    task?.status === Status.Completed && task.extracted_information !== null;
  const extractedInformation = showExtractedInformation ? (
    <div className="flex items-center">
      <Label className="w-32 shrink-0 text-lg">Extracted Information</Label>
      <Textarea
        rows={5}
        value={JSON.stringify(task.extracted_information, null, 2)}
        readOnly
      />
    </div>
  ) : null;

  const taskIsRunningOrQueued =
    task?.status === Status.Running || task?.status === Status.Queued;

  const showFailureReason =
    task?.status === Status.Failed ||
    task?.status === Status.Terminated ||
    task?.status === Status.TimedOut;
  const failureReason = showFailureReason ? (
    <div className="flex items-center">
      <Label className="w-32 shrink-0 text-lg">Failure Reason</Label>
      <Textarea
        rows={5}
        value={JSON.stringify(task.failure_reason, null, 2)}
        readOnly
      />
    </div>
  ) : null;

  return (
    <div className="flex flex-col gap-8">
      <div className="flex justify-between items-center">
        <div className="flex items-center gap-4">
          <span className="text-lg">{taskId}</span>
          {taskIsLoading ? (
            <Skeleton className="w-28 h-8" />
          ) : task ? (
            <StatusBadge status={task?.status} />
          ) : null}
        </div>
        {taskIsRunningOrQueued && (
          <Dialog>
            <DialogTrigger asChild>
              <Button variant="destructive">Cancel</Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Are you sure?</DialogTitle>
                <DialogDescription>
                  Are you sure you want to cancel this task?
                </DialogDescription>
              </DialogHeader>
              <DialogFooter>
                <DialogClose asChild>
                  <Button variant="secondary">Back</Button>
                </DialogClose>
                <Button
                  variant="destructive"
                  onClick={() => {
                    cancelTaskMutation.mutate();
                  }}
                  disabled={cancelTaskMutation.isPending}
                >
                  {cancelTaskMutation.isPending && (
                    <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                  )}
                  Cancel Task
                </Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>
        )}
      </div>
      {taskIsLoading ? (
        <div className="flex items-center gap-2">
          <Skeleton className="w-32 h-32" />
          <Skeleton className="w-full h-32" />
        </div>
      ) : (
        <>
          {extractedInformation}
          {failureReason}
        </>
      )}
      <div className="flex justify-center items-center">
        <div className="inline-flex border rounded bg-muted p-1">
          <NavLink
            to="actions"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer px-2 py-1 rounded-md text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Actions
          </NavLink>
          <NavLink
            to="recording"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer px-2 py-1 rounded-md text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Recording
          </NavLink>
          <NavLink
            to="parameters"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer px-2 py-1 rounded-md text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Parameters
          </NavLink>
          <NavLink
            to="diagnostics"
            className={({ isActive }) => {
              return cn(
                "cursor-pointer px-2 py-1 rounded-md text-muted-foreground",
                {
                  "bg-primary-foreground text-foreground": isActive,
                },
              );
            }}
          >
            Diagnostics
          </NavLink>
        </div>
      </div>
      <Outlet />
    </div>
  );
}

export { TaskDetails };
