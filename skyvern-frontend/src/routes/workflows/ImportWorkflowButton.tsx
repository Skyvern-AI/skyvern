import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { UploadIcon } from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { useId, useRef, useState } from "react";
import { parse as parseYAML, stringify as convertToYAML } from "yaml";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { toast } from "@/components/ui/use-toast";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { AxiosError } from "axios";

function isJsonString(str: string): boolean {
  try {
    JSON.parse(str);
  } catch {
    return false;
  }
  return true;
}

function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof AxiosError) {
    return error.response?.data?.detail || error.message || fallback;
  } else if (error instanceof Error) {
    return error.message;
  }
  return fallback;
}

function extractTitleFromYaml(yaml: string): string | null {
  try {
    const parsed = parseYAML(yaml);
    if (parsed && typeof parsed === "object" && "title" in parsed) {
      const title = (parsed as { title?: unknown }).title;
      if (typeof title === "string" && title.trim().length > 0) {
        return title.trim();
      }
    }
  } catch {
    return null;
  }
  return null;
}

type DuplicateReason =
  | { kind: "existing"; existingTitle: string }
  | { kind: "intra-batch" }
  | { kind: "check-failed" };

type PreparedYamlImport = {
  fileName: string;
  yaml: string;
  title: string | null;
  duplicateReason: DuplicateReason | null;
};

interface ImportWorkflowButtonProps {
  onImportStart?: () => void;
  selectedFolderId?: string | null;
}

function ImportWorkflowButton({
  onImportStart,
  selectedFolderId,
}: ImportWorkflowButtonProps) {
  const inputId = useId();
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [duplicateConfirmOpen, setDuplicateConfirmOpen] = useState(false);
  const [pendingDuplicates, setPendingDuplicates] = useState<
    PreparedYamlImport[]
  >([]);
  const [pendingNonDuplicates, setPendingNonDuplicates] = useState<
    PreparedYamlImport[]
  >([]);
  const [isImporting, setIsImporting] = useState(false);

  const createWorkflowFromYaml = async (
    yaml: string,
    fileName: string,
  ): Promise<boolean> => {
    try {
      const client = await getClient(credentialGetter);
      const params: Record<string, string> = {};
      if (selectedFolderId) {
        params.folder_id = selectedFolderId;
      }
      await client.post<string, { data: WorkflowApiResponse }>(
        "/workflows",
        yaml,
        {
          headers: {
            "Content-Type": "text/plain",
          },
          params,
        },
      );
      return true;
    } catch (error) {
      toast({
        variant: "destructive",
        title: `Error importing ${fileName}`,
        description: getErrorMessage(error, "Failed to import workflow"),
      });
      return false;
    }
  };

  const createWorkflowFromPdf = async (file: File): Promise<boolean> => {
    try {
      const formData = new FormData();
      formData.append("file", file);

      const client = await getClient(credentialGetter);
      const params: Record<string, string> = {};
      if (selectedFolderId) {
        params.folder_id = selectedFolderId;
      }
      await client.post("/workflows/import-pdf", formData, {
        headers: {
          "Content-Type": "multipart/form-data",
        },
        params,
      });
      return true;
    } catch (error) {
      toast({
        title: `Import Failed: ${file.name}`,
        description: getErrorMessage(error, "Failed to import PDF"),
        variant: "destructive",
      });
      return false;
    }
  };

  const findExistingWorkflowTitle = async (
    title: string,
  ): Promise<string | null> => {
    const pageSize = 50;
    const maxPages = 5;
    const normalized = title.trim().toLowerCase();
    const client = await getClient(credentialGetter);
    for (let page = 1; page <= maxPages; page++) {
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(pageSize));
      params.append("only_workflows", "true");
      params.append("search_key", title);
      const response = await client.get<Array<WorkflowApiResponse>>(
        "/workflows",
        { params },
      );
      const match = response.data.find(
        (wf) => wf.title.trim().toLowerCase() === normalized,
      );
      if (match) {
        return match.title;
      }
      if (response.data.length < pageSize) {
        return null;
      }
    }
    return null;
  };

  const importYamlFiles = async (files: PreparedYamlImport[]) => {
    if (files.length === 0) {
      return;
    }
    const results = await Promise.all(
      files.map((f) => createWorkflowFromYaml(f.yaml, f.fileName)),
    );
    const successCount = results.filter(Boolean).length;
    if (successCount > 0) {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["folders"] });
      toast({
        variant: "success",
        title:
          successCount === 1
            ? "Workflow imported"
            : `${successCount} workflows imported`,
        description:
          successCount === files.length
            ? "Successfully imported all workflows"
            : `${successCount} of ${files.length} workflows imported successfully`,
      });
    }
  };

  const handleFiles = async (fileList: FileList) => {
    const files = Array.from(fileList);
    const pdfFiles = files.filter((f) => f.name.toLowerCase().endsWith(".pdf"));
    const yamlLikeFiles = files.filter(
      (f) => !f.name.toLowerCase().endsWith(".pdf"),
    );

    let anyPdfStarted = false;
    for (const file of pdfFiles) {
      const ok = await createWorkflowFromPdf(file);
      if (ok) {
        anyPdfStarted = true;
        toast({
          title: "Import started",
          description: `Importing ${file.name}...`,
        });
      }
    }
    if (anyPdfStarted) {
      onImportStart?.();
    }

    const prepared: PreparedYamlImport[] = await Promise.all(
      yamlLikeFiles.map(async (file) => {
        const text = await file.text();
        const isJson = isJsonString(text);
        const yaml = isJson ? convertToYAML(JSON.parse(text)) : text;
        const title = extractTitleFromYaml(yaml);
        let duplicateReason: DuplicateReason | null = null;
        if (title) {
          try {
            const existing = await findExistingWorkflowTitle(title);
            if (existing) {
              duplicateReason = { kind: "existing", existingTitle: existing };
            }
          } catch {
            duplicateReason = { kind: "check-failed" };
          }
        }
        return {
          fileName: file.name,
          yaml,
          title,
          duplicateReason,
        };
      }),
    );

    const titleCounts = new Map<string, number>();
    for (const p of prepared) {
      if (!p.title) continue;
      const key = p.title.trim().toLowerCase();
      titleCounts.set(key, (titleCounts.get(key) ?? 0) + 1);
    }
    for (const p of prepared) {
      if (p.duplicateReason !== null || !p.title) continue;
      const key = p.title.trim().toLowerCase();
      if ((titleCounts.get(key) ?? 0) > 1) {
        p.duplicateReason = { kind: "intra-batch" };
      }
    }

    const checkFailed = prepared.filter(
      (p) => p.duplicateReason?.kind === "check-failed",
    );
    if (checkFailed.length > 0) {
      toast({
        variant: "destructive",
        title:
          checkFailed.length === 1
            ? "Could not verify duplicate"
            : `Could not verify ${checkFailed.length} duplicates`,
        description:
          "Network error checking if these workflows already exist. Review before importing.",
      });
    }

    const duplicates = prepared.filter((p) => p.duplicateReason !== null);
    const nonDuplicates = prepared.filter((p) => p.duplicateReason === null);

    if (duplicates.length === 0) {
      await importYamlFiles(nonDuplicates);
      return;
    }

    setPendingNonDuplicates(nonDuplicates);
    setPendingDuplicates(duplicates);
    setDuplicateConfirmOpen(true);
  };

  const handleConfirmImportDuplicates = async () => {
    const all = [...pendingNonDuplicates, ...pendingDuplicates];
    setDuplicateConfirmOpen(false);
    setPendingDuplicates([]);
    setPendingNonDuplicates([]);
    setIsImporting(true);
    try {
      await importYamlFiles(all);
    } finally {
      setIsImporting(false);
    }
  };

  const handleSkipDuplicates = async () => {
    const nonDupes = pendingNonDuplicates;
    setDuplicateConfirmOpen(false);
    setPendingDuplicates([]);
    setPendingNonDuplicates([]);
    setIsImporting(true);
    try {
      await importYamlFiles(nonDupes);
    } finally {
      setIsImporting(false);
    }
  };

  const handleCancelImport = () => {
    setDuplicateConfirmOpen(false);
    setPendingDuplicates([]);
    setPendingNonDuplicates([]);
  };

  const isBusy = isImporting || duplicateConfirmOpen;

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger>
            <Label htmlFor={inputId}>
              <input
                ref={inputRef}
                id={inputId}
                type="file"
                accept=".yaml,.yml,.json,.pdf"
                multiple
                className="hidden"
                disabled={isBusy}
                onChange={async (event) => {
                  const files = event.target.files;
                  if (!files || files.length === 0) {
                    return;
                  }
                  setIsImporting(true);
                  try {
                    await handleFiles(files);
                  } finally {
                    if (inputRef.current) {
                      inputRef.current.value = "";
                    }
                    setIsImporting(false);
                  }
                }}
              />
              <div
                className={`flex h-full items-center gap-2 rounded-md bg-secondary px-4 py-2 font-bold text-secondary-foreground ${
                  isBusy
                    ? "cursor-not-allowed opacity-60"
                    : "cursor-pointer hover:bg-secondary/90"
                }`}
              >
                <UploadIcon className="h-4 w-4" />
                {isImporting ? "Importing..." : "Import"}
              </div>
            </Label>
          </TooltipTrigger>
          <TooltipContent>
            Import one or more workflows from YAML, JSON, or PDF files
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <Dialog
        open={duplicateConfirmOpen}
        onOpenChange={(open) => {
          if (!open) {
            handleCancelImport();
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {pendingDuplicates.length === 1
                ? "Duplicate workflow detected"
                : "Duplicate workflows detected"}
            </DialogTitle>
            <DialogDescription asChild>
              <div className="space-y-3">
                <p>
                  {pendingDuplicates.length === 1
                    ? "The following file may create a duplicate:"
                    : "The following files may create duplicates:"}
                </p>
                <ul className="space-y-2 rounded-md border border-amber-500/50 bg-amber-500/10 p-3 text-sm">
                  {pendingDuplicates.map((dup) => (
                    <li key={dup.fileName}>
                      <div>
                        <span className="font-medium">{dup.fileName}</span>
                        {dup.title && (
                          <span className="text-muted-foreground">
                            {" "}
                            — &ldquo;{dup.title}&rdquo;
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-muted-foreground">
                        {dup.duplicateReason?.kind === "existing" &&
                          "A workflow with this title already exists"}
                        {dup.duplicateReason?.kind === "intra-batch" &&
                          "Another selected file has the same title"}
                        {dup.duplicateReason?.kind === "check-failed" &&
                          "Could not verify if a duplicate exists"}
                      </div>
                    </li>
                  ))}
                </ul>
                <p>
                  Would you like to import{" "}
                  {pendingDuplicates.length === 1 ? "it" : "them"} anyway?
                </p>
              </div>
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2 sm:gap-2">
            <Button variant="secondary" onClick={handleCancelImport}>
              Cancel
            </Button>
            {pendingNonDuplicates.length > 0 && (
              <Button variant="outline" onClick={handleSkipDuplicates}>
                Skip duplicates
              </Button>
            )}
            <Button onClick={handleConfirmImportDuplicates}>
              Import anyway
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

export { ImportWorkflowButton };
