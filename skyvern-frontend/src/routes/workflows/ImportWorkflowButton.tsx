import { getClient } from "@/api/AxiosClient";
import { Label } from "@/components/ui/label";
import { ReloadIcon, UploadIcon } from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useId } from "react";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { useNavigate } from "react-router-dom";
import { AxiosError } from "axios";
import { toast } from "@/components/ui/use-toast";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

function isJsonString(str: string): boolean {
  try {
    JSON.parse(str);
  } catch (e) {
    return false;
  }
  return true;
}

function ImportWorkflowButton() {
  const inputId = useId();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const createWorkflowFromYamlMutation = useMutation({
    mutationFn: async (yaml: string) => {
      const client = await getClient(credentialGetter);
      return client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        },
      );
    },
    onSuccess: (response) => {
      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      navigate(`/workflows/${response.data.workflow_permanent_id}/edit`);
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Error importing workflow",
        description: error.message || "An error occurred",
      });
    },
  });

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger>
          <Label htmlFor={inputId}>
            <input
              id={inputId}
              type="file"
              accept=".yaml,.yml,.json"
              className="hidden"
              onChange={async (event) => {
                if (event.target.files && event.target.files[0]) {
                  const fileTextContent = await event.target.files[0].text();
                  const isJson = isJsonString(fileTextContent);
                  const content = isJson
                    ? convertToYAML(JSON.parse(fileTextContent))
                    : fileTextContent;
                  createWorkflowFromYamlMutation.mutate(content);
                }
              }}
            />
            <div className="flex h-full cursor-pointer items-center gap-2 rounded-md bg-secondary px-4 py-2 font-bold text-secondary-foreground hover:bg-secondary/90">
              {createWorkflowFromYamlMutation.isPending ? (
                <ReloadIcon className="h-4 w-4 animate-spin" />
              ) : (
                <UploadIcon className="h-4 w-4" />
              )}
              Import
            </div>
          </Label>
        </TooltipTrigger>
        <TooltipContent>
          Import a workflow from a YAML or JSON file
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { ImportWorkflowButton };
