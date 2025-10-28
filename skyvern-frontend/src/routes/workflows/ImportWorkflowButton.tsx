import { getClient } from "@/api/AxiosClient";
import { Label } from "@/components/ui/label";
import { UploadIcon } from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { useId } from "react";
import { stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
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

interface ImportWorkflowButtonProps {
  onImportStart?: () => void;
}

function ImportWorkflowButton({ onImportStart }: ImportWorkflowButtonProps) {
  const inputId = useId();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();

  const createWorkflowFromYamlMutation = async (yaml: string) => {
    try {
      const client = await getClient(credentialGetter);
      await client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
        }
      );

      queryClient.invalidateQueries({
        queryKey: ["workflows"],
      });
      toast({
        title: "Workflow imported",
        description: "Successfully imported workflow",
      });
    } catch (error) {
      const err =
        error instanceof Error ? error : new Error("Failed to import workflow");
      toast({
        variant: "destructive",
        title: "Error importing workflow",
        description: err.message,
      });
    }
  };

  const createWorkflowFromPdfMutation = async (file: File) => {
    try {
      const formData = new FormData();
      formData.append("file", file);

      const client = await getClient(credentialGetter);
      await client.post("/workflows/import-pdf", formData, {
        headers: {
          "Content-Type": "multipart/form-data",
        },
      });

      // Notify parent to start polling
      onImportStart?.();

      toast({
        title: "Import started",
        description: `Importing ${file.name}...`,
      });
    } catch (error) {
      const err =
        error instanceof Error ? error : new Error("Failed to import PDF");
      toast({
        title: "Import Failed",
        description: err.message,
        variant: "destructive",
      });
    }
  };

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
                    // Handle PDF file
                    await createWorkflowFromPdfMutation(file);
                  } else {
                    // Non-pdf files like yaml, json
                    const fileTextContent = await file.text();
                    const isJson = isJsonString(fileTextContent);
                    const content = isJson
                      ? convertToYAML(JSON.parse(fileTextContent))
                      : fileTextContent;

                    await createWorkflowFromYamlMutation(content);
                  }
                }
              }}
            />
            <div className="flex h-full cursor-pointer items-center gap-2 rounded-md bg-secondary px-4 py-2 font-bold text-secondary-foreground hover:bg-secondary/90">
              <UploadIcon className="h-4 w-4" />
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
