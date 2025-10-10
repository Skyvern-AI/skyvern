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
      navigate(`/workflows/${response.data.workflow_permanent_id}/debug`);
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
              accept=".yaml,.yml,.json,.pdf"
              className="hidden"
              onChange={async (event) => {
                if (event.target.files && event.target.files[0]) {
                  const file = event.target.files[0];
                  const fileName = file.name.toLowerCase();

                  if (fileName.endsWith(".pdf")) {
                    // Handle PDF file - send as FormData to new endpoint
                    const formData = new FormData();
                    formData.append("file", file);

                    const client = await getClient(credentialGetter);
                    try {
                      const response = await client.post<WorkflowApiResponse>(
                        "/workflows/import-pdf",
                        formData,
                        {
                          headers: {
                            "Content-Type": "multipart/form-data",
                          },
                        },
                      );

                      queryClient.invalidateQueries({
                        queryKey: ["workflows"],
                      });
                      navigate(
                        `/workflows/${response.data.workflow_permanent_id}/debug`,
                      );
                    } catch (error) {
                      toast({
                        title: "Import Failed",
                        description:
                          error instanceof Error
                            ? error.message
                            : "Failed to import PDF",
                        variant: "destructive",
                      });
                    }
                  } else {
                    // Non-pdf files like yaml, json
                    const fileTextContent = await file.text();
                    const isJson = isJsonString(fileTextContent);
                    const content = isJson
                      ? convertToYAML(JSON.parse(fileTextContent))
                      : fileTextContent;
                    createWorkflowFromYamlMutation.mutate(content);
                  }
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
          Import a workflow from a YAML, JSON, or PDF file
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

export { ImportWorkflowButton };
