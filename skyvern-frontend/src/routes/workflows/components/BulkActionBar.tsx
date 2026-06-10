import { getClient } from "@/api/AxiosClient";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { GarbageIcon } from "@/components/icons/GarbageIcon";
import { SelectionBar, SelectionBarDivider } from "@/components/SelectionBar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuPortal,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { bulkResultToast } from "@/util/bulkResultToast";
import {
  BULK_CONCURRENCY_LIMIT,
  runWithConcurrency,
} from "@/util/runWithConcurrency";
import {
  BookmarkFilledIcon,
  BookmarkIcon,
  CopyIcon,
  DotsHorizontalIcon,
  DownloadIcon,
} from "@radix-ui/react-icons";
import { useQueryClient } from "@tanstack/react-query";
import { stringify as convertToYAML } from "yaml";
import { convert } from "../editor/workflowEditorUtils";
import { Tag, TagKey } from "../types/tagTypes";
import { WorkflowApiResponse } from "../types/workflowTypes";
import { BulkTagPicker } from "./tagging/BulkTagPicker";
import { WorkflowFolderSelector } from "./WorkflowFolderSelector";

type Props = {
  selectedWorkflows: WorkflowApiResponse[];
  isOperating: boolean;
  onOperatingChange: (operating: boolean) => void;
  onClearSelection: () => void;
  onDeleteRequest: () => void;
  onMoveToFolder: (folderId: string | null) => Promise<void>;
  tagKeys: Array<TagKey>;
  labelSuggestions: Array<string>;
  valueSuggestionsByKey?: Map<string, Array<string>>;
};

// Blob URLs avoid the ~2MB data-URI cap that truncates large workflow exports.
function downloadFile(fileName: string, contents: string) {
  const blob = new Blob([contents], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const element = document.createElement("a");
  element.href = url;
  element.download = fileName;
  element.style.display = "none";
  document.body.appendChild(element);
  element.click();
  document.body.removeChild(element);
  // Deferred so a still-starting download cannot lose its blob.
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

function BulkActionBar({
  selectedWorkflows,
  isOperating,
  onOperatingChange,
  onClearSelection,
  onDeleteRequest,
  onMoveToFolder,
  tagKeys,
  labelSuggestions,
  valueSuggestionsByKey,
}: Props) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const count = selectedWorkflows.length;
  const nonTemplates = selectedWorkflows.filter(
    (workflow) => !workflow.is_template,
  );
  const templates = selectedWorkflows.filter(
    (workflow) => workflow.is_template,
  );

  async function handleBulkClone() {
    onOperatingChange(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        selectedWorkflows.map((workflow) => () => {
          const yaml = convertToYAML(
            convert({
              ...workflow,
              title: `Copy of ${workflow.title}`,
            }),
          );
          return client.post("/workflows", yaml, {
            headers: { "Content-Type": "text/plain" },
          });
        }),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      bulkResultToast({
        succeeded,
        total: count,
        results,
        successTitle: (n) => `Cloned ${n} agent${n !== 1 ? "s" : ""}.`,
        failureTitle: (n) => `Failed to clone ${n} agent${n !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `Cloned ${successCount} agent${successCount !== 1 ? "s" : ""}. ${failedCount} failed.`,
      });
      if (succeeded === count) {
        onClearSelection();
      }
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
        queryClient.invalidateQueries({ queryKey: ["folders"] });
      }
    } finally {
      onOperatingChange(false);
    }
  }

  async function handleBulkTemplateUpdate(
    workflows: WorkflowApiResponse[],
    isTemplate: boolean,
  ) {
    if (workflows.length === 0) {
      return;
    }
    onOperatingChange(true);
    try {
      // Template endpoint only exists on /v1 (no /api prefix)
      const client = await getClient(credentialGetter, "sans-api-v1");
      const results = await runWithConcurrency(
        workflows.map(
          (workflow) => () =>
            client.put(
              `/workflows/${workflow.workflow_permanent_id}/template?is_template=${isTemplate}`,
            ),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      bulkResultToast({
        succeeded,
        total: workflows.length,
        results,
        successTitle: (n) =>
          isTemplate
            ? `Saved ${n} agent${n !== 1 ? "s" : ""} as templates.`
            : `Removed ${n} agent${n !== 1 ? "s" : ""} from templates.`,
        failureTitle: (n) =>
          isTemplate
            ? `Failed to save ${n} agent${n !== 1 ? "s" : ""} as templates.`
            : `Failed to remove ${n} agent${n !== 1 ? "s" : ""} from templates.`,
        partialTitle: (successCount, failedCount) =>
          isTemplate
            ? `Saved ${successCount} agent${successCount !== 1 ? "s" : ""} as templates. ${failedCount} failed.`
            : `Removed ${successCount} agent${successCount !== 1 ? "s" : ""} from templates. ${failedCount} failed.`,
      });
      if (succeeded === workflows.length) {
        onClearSelection();
      }
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
        queryClient.invalidateQueries({ queryKey: ["orgTemplates"] });
      }
    } finally {
      onOperatingChange(false);
    }
  }

  async function handleBulkTagApply(tag: Tag) {
    onOperatingChange(true);
    try {
      const client = await getClient(credentialGetter);
      const results = await runWithConcurrency(
        selectedWorkflows.map(
          (workflow) => () =>
            client.post(`/workflows/${workflow.workflow_permanent_id}/tags`, {
              tags: [tag],
            }),
        ),
        BULK_CONCURRENCY_LIMIT,
      );
      const succeeded = results.filter((r) => r.status === "fulfilled").length;
      const tagLabel =
        tag.key !== null ? `${tag.key}: ${tag.value}` : tag.value;
      bulkResultToast({
        succeeded,
        total: count,
        results,
        successTitle: (n) =>
          `Tagged ${n} agent${n !== 1 ? "s" : ""} with ${tagLabel}.`,
        failureTitle: (n) => `Failed to tag ${n} agent${n !== 1 ? "s" : ""}.`,
        partialTitle: (successCount, failedCount) =>
          `Tagged ${successCount} agent${successCount !== 1 ? "s" : ""}. ${failedCount} failed.`,
      });
      // Tagging is additive; the selection stays for more tags or follow-up actions.
      if (succeeded > 0) {
        queryClient.invalidateQueries({ queryKey: ["workflow-tags"] });
        queryClient.invalidateQueries({ queryKey: ["tag-keys"] });
        queryClient.invalidateQueries({ queryKey: ["workflows"] });
      }
    } finally {
      onOperatingChange(false);
    }
  }

  function handleBulkExport(type: "json" | "yaml") {
    if (selectedWorkflows.length === 1) {
      const workflow = selectedWorkflows[0]!;
      const safeTitle = workflow.title.replace(/[/\\:*?"<>|]/g, "_");
      const contents =
        type === "json"
          ? JSON.stringify(convert(workflow), null, 2)
          : convertToYAML(convert(workflow));
      downloadFile(`${safeTitle}.${type}`, contents);
      return;
    }
    // One bundled file; per-agent downloads trip the browser's
    // multiple-downloads permission prompt.
    const converted = selectedWorkflows.map((workflow) => convert(workflow));
    const contents =
      type === "json"
        ? JSON.stringify(converted, null, 2)
        : converted
            .map((definition) => convertToYAML(definition))
            .join("---\n");
    downloadFile(`agents-export-${selectedWorkflows.length}.${type}`, contents);
  }

  return (
    <SelectionBar
      count={count}
      isOperating={isOperating}
      onClear={onClearSelection}
    >
      <WorkflowFolderSelector
        currentFolderId={null}
        bulkCount={count}
        onBulkFolderSelect={onMoveToFolder}
        bulkHasFolders={selectedWorkflows.some(
          (workflow) => workflow.folder_id !== null,
        )}
        disabled={isOperating}
        trigger={
          <Button size="sm" variant="ghost" disabled={isOperating}>
            <FolderIcon className="mr-1.5 h-4 w-4" />
            Move to folder
          </Button>
        }
      />
      <BulkTagPicker
        bulkCount={count}
        tagKeys={tagKeys}
        labelSuggestions={labelSuggestions}
        valueSuggestionsByKey={valueSuggestionsByKey}
        disabled={isOperating}
        onApplyTag={handleBulkTagApply}
      />
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button size="sm" variant="ghost" disabled={isOperating}>
            <DotsHorizontalIcon className="mr-1.5 h-4 w-4" />
            More
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" side="top">
          <DropdownMenuItem
            className="p-2"
            onSelect={() => void handleBulkClone()}
          >
            <CopyIcon className="mr-2 h-4 w-4" />
            Clone
          </DropdownMenuItem>
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <BookmarkIcon className="mr-2 h-4 w-4" />
              Template
            </DropdownMenuSubTrigger>
            <DropdownMenuPortal>
              <DropdownMenuSubContent>
                <DropdownMenuItem
                  disabled={nonTemplates.length === 0}
                  onSelect={() =>
                    void handleBulkTemplateUpdate(nonTemplates, true)
                  }
                >
                  <BookmarkIcon className="mr-2 h-4 w-4" />
                  Save as Templates ({nonTemplates.length})
                </DropdownMenuItem>
                <DropdownMenuItem
                  disabled={templates.length === 0}
                  onSelect={() =>
                    void handleBulkTemplateUpdate(templates, false)
                  }
                >
                  <BookmarkFilledIcon className="mr-2 h-4 w-4" />
                  Remove from Templates ({templates.length})
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuPortal>
          </DropdownMenuSub>
          <DropdownMenuSub>
            <DropdownMenuSubTrigger>
              <DownloadIcon className="mr-2 h-4 w-4" />
              Export as...
            </DropdownMenuSubTrigger>
            <DropdownMenuPortal>
              <DropdownMenuSubContent>
                <DropdownMenuItem onSelect={() => handleBulkExport("yaml")}>
                  YAML
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={() => handleBulkExport("json")}>
                  JSON
                </DropdownMenuItem>
              </DropdownMenuSubContent>
            </DropdownMenuPortal>
          </DropdownMenuSub>
        </DropdownMenuContent>
      </DropdownMenu>
      <SelectionBarDivider />
      <Button
        size="sm"
        variant="ghost"
        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
        onClick={onDeleteRequest}
        disabled={isOperating}
      >
        <GarbageIcon className="mr-1.5 h-4 w-4" />
        Delete
      </Button>
    </SelectionBar>
  );
}

export { BulkActionBar };
