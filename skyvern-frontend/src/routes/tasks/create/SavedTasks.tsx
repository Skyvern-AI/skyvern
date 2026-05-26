import { getClient } from "@/api/AxiosClient";
import { queryClient } from "@/api/QueryClient";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { PlusIcon, ReloadIcon } from "@radix-ui/react-icons";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { stringify as convertToYAML } from "yaml";
import { SavedTaskCard } from "./SavedTaskCard";
import { useState } from "react";
import { cn } from "@/util/utils";
import { Skeleton } from "@/components/ui/skeleton";
import {
  TaskBlock,
  WorkflowApiResponse,
} from "@/routes/workflows/types/workflowTypes";

function createEmptyTaskTemplate() {
  return {
    title: "New Template",
    description: "",
    is_saved_task: true,
    webhook_callback_url: null,
    proxy_location: "RESIDENTIAL",
    workflow_definition: {
      version: 2,
      parameters: [
        {
          parameter_type: "workflow",
          workflow_parameter_type: "json",
          key: "navigation_payload",
          default_value: "null",
        },
      ],
      blocks: [
        {
          block_type: "task",
          label: "New Template",
          url: "https://example.com",
          navigation_goal: "",
          data_extraction_goal: null,
          data_schema: null,
        },
      ],
    },
  };
}

function SavedTasks() {
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const [hovering, setHovering] = useState(false);

  const { data, isLoading: savedTasksIsLoading } = useQuery<
    Array<WorkflowApiResponse>
  >({
    queryKey: ["savedTasks"],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      return client
        .get("/workflows?only_saved_tasks=true")
        .then((response) => response.data);
    },
  });

  const mutation = useMutation({
    mutationFn: async () => {
      const request = createEmptyTaskTemplate();
      const client = await getClient(credentialGetter);
      const yaml = convertToYAML(request);
      return client
        .post<string, { data: { workflow_permanent_id: string } }>(
          "/workflows",
          yaml,
          {
            headers: {
              "Content-Type": "text/plain",
            },
          },
        )
        .then((response) => response.data);
    },
    onError: (error) => {
      toast({
        variant: "destructive",
        title: "There was an error while saving changes",
        description: error.message,
      });
    },
    onSuccess: (response) => {
      toast({
        variant: "success",
        title: "New template created",
        description: "Your template was created successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["savedTasks"],
      });
      navigate(`/tasks/create/${response.workflow_permanent_id}`);
    },
  });

  return (
    <div className="grid grid-cols-4 gap-4">
      <Card
        className="border-0"
        onMouseEnter={() => setHovering(true)}
        onMouseLeave={() => setHovering(false)}
        onMouseOver={() => setHovering(true)}
        onMouseOut={() => setHovering(false)}
      >
        <CardHeader
          className={cn("rounded-t-md bg-slate-elevation1", {
            "bg-slate-900": hovering,
          })}
        >
          <CardTitle className="font-normal">New Task</CardTitle>
          <CardDescription>{"https://.."}</CardDescription>
        </CardHeader>
        <CardContent
          className={cn(
            "flex h-36 cursor-pointer items-center justify-center rounded-b-md bg-slate-elevation3 p-4 text-sm text-slate-300",
            {
              "bg-slate-800": hovering,
            },
          )}
          onClick={() => {
            if (mutation.isPending) {
              return;
            }
            mutation.mutate();
          }}
        >
          {!mutation.isPending && <PlusIcon className="h-12 w-12" />}
          {mutation.isPending && (
            <ReloadIcon className="h-12 w-12 animate-spin" />
          )}
        </CardContent>
      </Card>
      {savedTasksIsLoading && (
        <>
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
          <Skeleton className="h-56" />
        </>
      )}
      {data?.map((workflow) => {
        const firstBlock = workflow.workflow_definition.blocks[0];
        if (!firstBlock || firstBlock.block_type !== "task") {
          return null; // saved tasks have only one block and it's a task
        }
        const task = firstBlock as TaskBlock;
        return (
          <SavedTaskCard
            key={workflow.workflow_permanent_id}
            workflowId={workflow.workflow_permanent_id}
            title={workflow.title}
            description={workflow.description}
            url={task.url ?? ""}
          />
        );
      })}
    </div>
  );
}

export { SavedTasks };
