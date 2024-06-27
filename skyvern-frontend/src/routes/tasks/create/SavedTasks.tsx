import { getClient } from "@/api/AxiosClient";
import { queryClient } from "@/api/QueryClient";
import { WorkflowApiResponse } from "@/api/types";
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

function createEmptyTaskTemplate() {
  return {
    title: "New Template",
    description: "",
    is_saved_task: true,
    webhook_callback_url: null,
    proxy_location: "RESIDENTIAL",
    workflow_definition: {
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

  const { data } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows"],
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
        title: "New template created",
        description: "Your template was created successfully",
      });
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      navigate(`/create/${response.workflow_permanent_id}`);
    },
  });

  return (
    <div className="grid grid-cols-4 gap-4">
      <Card
        onClick={() => {
          if (mutation.isPending) {
            return;
          }
          mutation.mutate();
        }}
      >
        <CardHeader>
          <CardTitle>New Template</CardTitle>
          <CardDescription className="whitespace-nowrap overflow-hidden text-ellipsis">
            Create your own template
          </CardDescription>
        </CardHeader>
        <CardContent className="flex h-48 justify-center items-center hover:bg-muted/40 cursor-pointer">
          {!mutation.isPending && <PlusIcon className="w-12 h-12" />}
          {mutation.isPending && (
            <ReloadIcon className="animate-spin w-12 h-12" />
          )}
        </CardContent>
      </Card>
      {data?.map((workflow) => {
        return (
          <SavedTaskCard
            key={workflow.workflow_permanent_id}
            workflowId={workflow.workflow_permanent_id}
            title={workflow.title}
            description={workflow.description}
            url={workflow.workflow_definition.blocks[0]?.url ?? ""}
          />
        );
      })}
    </div>
  );
}

export { SavedTasks };
