import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  BookmarkFilledIcon,
  ChevronDownIcon,
  DotsHorizontalIcon,
  FileIcon,
  LightningBoltIcon,
  MixerHorizontalIcon,
  Pencil2Icon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import React, { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { NarrativeCard } from "./components/header/NarrativeCard";
import { FolderCard } from "./components/FolderCard";
import { CreateFolderDialog } from "./components/CreateFolderDialog";
import { CreateFromTemplateDialog } from "./components/CreateFromTemplateDialog";
import { ViewAllFoldersDialog } from "./components/ViewAllFoldersDialog";
import { WorkflowFolderSelector } from "./components/WorkflowFolderSelector";
import { HighlightText } from "./components/HighlightText";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { useFoldersQuery } from "./hooks/useFoldersQuery";
import { useActiveImportsPolling } from "./hooks/useActiveImportsPolling";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { convert } from "./editor/workflowEditorUtils";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";
import { Skeleton } from "@/components/ui/skeleton";
import { TableSearchInput } from "@/components/TableSearchInput";
import { ParameterDisplayInline } from "./components/ParameterDisplayInline";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";

const emptyWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Workflow",
  description: "",
  ai_fallback: true,
  run_with: "agent",
  workflow_definition: {
    version: 2,
    blocks: [],
    parameters: [],
  },
};

function Workflows() {
  const credentialGetter = useCredentialGetter();
  const navigate = useNavigate();
  const createWorkflowMutation = useCreateWorkflowMutation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 250);
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;

  // Folder state
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [isViewAllFoldersOpen, setIsViewAllFoldersOpen] = useState(false);

  // Template dialog state
  const [isTemplateDialogOpen, setIsTemplateDialogOpen] = useState(false);

  // Poll for active imports
  const { activeImports, startPolling } = useActiveImportsPolling();

  // Fetch folders
  const { data: allFolders = [] } = useFoldersQuery({ page_size: 10 });

  // Create folders map for O(1) lookup
  const foldersMap = useMemo(() => {
    return new Map(allFolders.map((f) => [f.folder_id, f]));
  }, [allFolders]);

  // Sort folders by modified date (most recent first) and get top 5
  const recentFolders = useMemo(() => {
    return [...allFolders]
      .sort(
        (a, b) =>
          new Date(b.modified_at).getTime() - new Date(a.modified_at).getTime(),
      )
      .slice(0, 5);
  }, [allFolders]);

  const {
    data: workflows = [],
    isFetching,
    isPlaceholderData,
  } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: [
      "workflows",
      debouncedSearch,
      page,
      itemsPerPage,
      selectedFolderId,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page));
      params.append("page_size", String(itemsPerPage));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      if (selectedFolderId) {
        params.append("folder_id", selectedFolderId);
      }
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
    placeholderData: (previousData) => previousData,
  });

  const { data: nextPageWorkflows } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: [
      "workflows",
      debouncedSearch,
      page + 1,
      itemsPerPage,
      selectedFolderId,
    ],
    queryFn: async () => {
      const client = await getClient(credentialGetter);
      const params = new URLSearchParams();
      params.append("page", String(page + 1));
      params.append("page_size", String(itemsPerPage));
      params.append("only_workflows", "true");
      if (debouncedSearch) {
        params.append("search_key", debouncedSearch);
      }
      if (selectedFolderId) {
        params.append("folder_id", selectedFolderId);
      }
      return client
        .get(`/workflows`, {
          params,
        })
        .then((response) => response.data);
    },
    enabled: workflows.length === itemsPerPage,
  });

  const isNextDisabled =
    isFetching || !nextPageWorkflows || nextPageWorkflows.length === 0;

  const { matchesParameter, isSearchActive } =
    useKeywordSearch(debouncedSearch);
  const {
    expandedRows,
    toggleExpanded: toggleParametersExpanded,
    setAutoExpandedRows,
  } = useParameterExpansion();

  useEffect(() => {
    if (!isSearchActive) {
      setAutoExpandedRows([]);
      return;
    }

    const matchingWorkflows = workflows.filter((workflow) =>
      workflow.workflow_definition.parameters?.some((param) => {
        const value =
          param.parameter_type === "workflow" ? param.default_value : undefined;
        return matchesParameter({
          key: param.key,
          value,
          description: param.description ?? null,
        });
      }),
    );

    setAutoExpandedRows(
      matchingWorkflows.map((workflow) => workflow.workflow_permanent_id),
    );
  }, [isSearchActive, workflows, matchesParameter, setAutoExpandedRows]);

  function handleRowClick(
    event: React.MouseEvent<HTMLTableCellElement>,
    workflowPermanentId: string,
  ) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + `/workflows/${workflowPermanentId}/runs`,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    navigate(`/workflows/${workflowPermanentId}/runs`);
  }

  function handleIconClick(
    event: React.MouseEvent<HTMLButtonElement>,
    path: string,
  ) {
    if (event.ctrlKey || event.metaKey) {
      window.open(
        window.location.origin + path,
        "_blank",
        "noopener,noreferrer",
      );
      return;
    }
    navigate(path);
  }

  function setParamPatch(patch: Record<string, string>) {
    const params = new URLSearchParams(searchParams);
    Object.entries(patch).forEach(([k, v]) => params.set(k, v));
    setSearchParams(params, { replace: true });
  }

  function handlePreviousPage() {
    if (page === 1) return;
    setParamPatch({ page: String(page - 1) });
  }

  function handleNextPage() {
    if (isNextDisabled) return;
    setParamPatch({ page: String(page + 1) });
  }

  // Show importing workflows from polling hook (only on page 1)
  const displayWorkflows = useMemo(() => {
    const importingOnly = activeImports.filter(
      (imp) => imp.status === "importing",
    );

    if (page === 1 && importingOnly.length > 0) {
      return [...importingOnly, ...workflows];
    }
    return workflows;
  }, [activeImports, workflows, page]);

  return (
    <div className="space-y-10">
      <div className="flex h-32 justify-between gap-6">
        <div className="space-y-5">
          <div className="flex items-center gap-2">
            <LightningBoltIcon className="size-6" />
            <h1 className="text-2xl">Workflows</h1>
          </div>
          <p className="text-slate-300">
            Create your own complex workflows by connecting web agents together.
            Define a series of actions, set it, and forget it.
          </p>
        </div>
        <div className="flex gap-5">
          <NarrativeCard
            index={1}
            description="Save browser sessions and reuse them in subsequent runs"
          />
          <NarrativeCard
            index={2}
            description="Connect multiple agents together to carry out complex objectives"
          />
          <NarrativeCard
            index={3}
            description="Execute non-browser tasks such as sending emails"
          />
        </div>
      </div>
      <div className="space-y-4">
        {/* Folders Section */}
        <div className="space-y-4">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-3">
              <h2 className="text-lg font-semibold">Folders</h2>
              <Button
                variant="link"
                size="sm"
                className="h-auto p-0 text-blue-600 dark:text-blue-400"
                onClick={() => setIsCreateFolderOpen(true)}
              >
                + New folder
              </Button>
            </div>
            {allFolders.length > 5 && (
              <Button
                variant="link"
                size="sm"
                className="text-blue-600 dark:text-blue-400"
                onClick={() => setIsViewAllFoldersOpen(true)}
              >
                View all
              </Button>
            )}
          </div>

          {recentFolders.length > 0 ? (
            <div className="grid grid-cols-5 gap-4">
              {recentFolders.map((folder) => (
                <FolderCard
                  key={folder.folder_id}
                  folder={folder}
                  isSelected={selectedFolderId === folder.folder_id}
                  onClick={() =>
                    setSelectedFolderId(
                      selectedFolderId === folder.folder_id
                        ? null
                        : folder.folder_id,
                    )
                  }
                />
              ))}
            </div>
          ) : (
            <div className="rounded-lg border border-slate-200 bg-slate-elevation1 py-6 text-center dark:border-slate-700">
              <div className="mx-auto max-w-md">
                <FileIcon className="mx-auto mb-3 h-10 w-10 text-blue-400 opacity-50" />
                <h3 className="mb-2 text-slate-900 dark:text-slate-100">
                  Organize Your Workflows with Folders
                </h3>
                <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
                  Keep your workflows organized by creating folders. Group
                  related workflows together by project, team, or workflow type
                  for easier management.
                </p>
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0 text-blue-600 dark:text-blue-400"
                  onClick={() => setIsCreateFolderOpen(true)}
                >
                  <PlusIcon className="mr-2 h-4 w-4" />
                  Create Your First Folder
                </Button>
              </div>
            </div>
          )}
        </div>

        {/* Workflows Section */}
        <header className="flex items-center justify-between">
          <h1 className="text-xl">My Flows</h1>
          {selectedFolderId && (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setSelectedFolderId(null)}
            >
              View all workflows
            </Button>
          )}
        </header>
        <div className="flex justify-between">
          <TableSearchInput
            value={search}
            onChange={(value) => {
              setSearch(value);
              setParamPatch({ page: "1" });
            }}
            placeholder="Search by title or parameter..."
            className="w-48 lg:w-72"
          />
          <div className="flex gap-4">
            <ImportWorkflowButton
              onImportStart={startPolling}
              selectedFolderId={selectedFolderId}
            />
            <DropdownMenu modal={false}>
              <DropdownMenuTrigger asChild>
                <Button disabled={createWorkflowMutation.isPending}>
                  {createWorkflowMutation.isPending ? (
                    <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                  ) : (
                    <PlusIcon className="mr-2 h-4 w-4" />
                  )}
                  Create
                  <ChevronDownIcon className="ml-2 h-4 w-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  onSelect={() => {
                    createWorkflowMutation.mutate({
                      ...emptyWorkflowRequest,
                      folder_id: selectedFolderId,
                    });
                  }}
                >
                  <PlusIcon className="mr-2 h-4 w-4" />
                  Blank Workflow
                </DropdownMenuItem>
                <DropdownMenuItem
                  onSelect={() => setIsTemplateDialogOpen(true)}
                >
                  <BookmarkFilledIcon className="mr-2 h-4 w-4" />
                  From Template
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
        <div className="rounded-lg border">
          <Table>
            <TableHeader className="rounded-t-lg bg-slate-elevation2">
              <TableRow>
                <TableHead className="w-1/4 rounded-tl-lg text-slate-400">
                  ID
                </TableHead>
                <TableHead className="w-1/4 text-slate-400">Title</TableHead>
                <TableHead className="w-1/6 text-slate-400">Folder</TableHead>
                <TableHead className="w-1/6 text-slate-400">
                  Created At
                </TableHead>
                <TableHead className="rounded-tr-lg"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {isFetching &&
              !isPlaceholderData &&
              displayWorkflows.length === 0 ? (
                // Show skeleton rows only on initial load (not during search refinement)
                Array.from({ length: 10 }).map((_, index) => (
                  <TableRow key={`skeleton-${index}`}>
                    <TableCell>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                    <TableCell>
                      <Skeleton className="h-5 w-full" />
                    </TableCell>
                    <TableCell>
                      <Skeleton className="h-5 w-20" />
                    </TableCell>
                    <TableCell>
                      <Skeleton className="h-5 w-32" />
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-2">
                        <Skeleton className="h-8 w-8 rounded" />
                        <Skeleton className="h-8 w-8 rounded" />
                        <Skeleton className="h-8 w-8 rounded" />
                        <Skeleton className="h-8 w-8 rounded" />
                      </div>
                    </TableCell>
                  </TableRow>
                ))
              ) : displayWorkflows?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5}>No workflows found</TableCell>
                </TableRow>
              ) : (
                displayWorkflows?.map((workflow) => {
                  const parameterItems = workflow.workflow_definition.parameters
                    .filter((p) => p.parameter_type !== "output")
                    .map((param) => ({
                      key: param.key,
                      value:
                        param.parameter_type === "workflow"
                          ? param.default_value ?? ""
                          : "",
                      description: param.description ?? null,
                    }));
                  const hasParameters = parameterItems.length > 0;
                  const isExpanded = expandedRows.has(
                    workflow.workflow_permanent_id,
                  );
                  // Check if this is an importing workflow
                  const isUploading = workflow.status === "importing";

                  return (
                    <React.Fragment key={workflow.workflow_permanent_id}>
                      {/* Main workflow row */}
                      {isUploading ? (
                        <TableRow className="opacity-70">
                          <TableCell colSpan={2}>
                            <div className="flex items-center gap-2">
                              <ReloadIcon className="h-4 w-4 animate-spin text-blue-400" />
                              <span>{workflow.title}</span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <span className="text-slate-400">-</span>
                          </TableCell>
                          <TableCell>
                            {basicLocalTimeFormat(workflow.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <Button size="icon" variant="ghost" disabled>
                                <FileIcon className="h-4 w-4" />
                              </Button>
                              <Button size="icon" variant="ghost" disabled>
                                <MixerHorizontalIcon className="h-4 w-4" />
                              </Button>
                              <Button size="icon" variant="ghost" disabled>
                                <PlayIcon className="h-4 w-4" />
                              </Button>
                              <Button size="icon" variant="ghost" disabled>
                                <DotsHorizontalIcon className="h-4 w-4" />
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ) : (
                        <TableRow className="cursor-pointer">
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id,
                              );
                            }}
                          >
                            <HighlightText
                              text={workflow.workflow_permanent_id}
                              query={debouncedSearch}
                            />
                          </TableCell>
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id,
                              );
                            }}
                          >
                            <div className="flex items-center gap-2">
                              <HighlightText
                                text={workflow.title}
                                query={debouncedSearch}
                              />
                              {workflow.is_template && (
                                <TooltipProvider>
                                  <Tooltip>
                                    <TooltipTrigger asChild>
                                      <BookmarkFilledIcon className="h-3.5 w-3.5 shrink-0 text-blue-500" />
                                    </TooltipTrigger>
                                    <TooltipContent>Template</TooltipContent>
                                  </Tooltip>
                                </TooltipProvider>
                              )}
                            </div>
                          </TableCell>
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id,
                              );
                            }}
                          >
                            {workflow.folder_id ? (
                              <div className="flex items-center gap-1.5">
                                <FileIcon className="h-3.5 w-3.5 text-blue-400" />
                                <span className="text-sm">
                                  <HighlightText
                                    text={
                                      foldersMap.get(workflow.folder_id)
                                        ?.title || workflow.folder_id
                                    }
                                    query={debouncedSearch}
                                  />
                                </span>
                              </div>
                            ) : (
                              <span className="text-slate-400">-</span>
                            )}
                          </TableCell>
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id,
                              );
                            }}
                            title={basicTimeFormat(workflow.created_at)}
                          >
                            {basicLocalTimeFormat(workflow.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <div>
                                      <WorkflowFolderSelector
                                        workflowPermanentId={
                                          workflow.workflow_permanent_id
                                        }
                                        currentFolderId={workflow.folder_id}
                                      />
                                    </div>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    Assign to Folder
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="outline"
                                      onClick={() =>
                                        toggleParametersExpanded(
                                          workflow.workflow_permanent_id,
                                        )
                                      }
                                      disabled={!hasParameters}
                                      className={cn(
                                        isExpanded && "text-blue-400",
                                      )}
                                    >
                                      <MixerHorizontalIcon className="h-4 w-4" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    {hasParameters
                                      ? isExpanded
                                        ? "Hide Parameters"
                                        : "Show Parameters"
                                      : "No Parameters"}
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="outline"
                                      onClick={(event) => {
                                        handleIconClick(
                                          event,
                                          `/workflows/${workflow.workflow_permanent_id}/debug`,
                                        );
                                      }}
                                    >
                                      <Pencil2Icon className="h-4 w-4" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    Open in Editor
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="outline"
                                      onClick={(event) => {
                                        handleIconClick(
                                          event,
                                          `/workflows/${workflow.workflow_permanent_id}/run`,
                                        );
                                      }}
                                    >
                                      <PlayIcon className="h-4 w-4" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>
                                    Create New Run
                                  </TooltipContent>
                                </Tooltip>
                              </TooltipProvider>
                              <WorkflowActions workflow={workflow} />
                            </div>
                          </TableCell>
                        </TableRow>
                      )}

                      {/* Expanded parameters section */}
                      {isExpanded && hasParameters && (
                        <TableRow
                          key={`${workflow.workflow_permanent_id}-params`}
                        >
                          <TableCell
                            colSpan={5}
                            className="bg-slate-50 dark:bg-slate-900/50"
                          >
                            <ParameterDisplayInline
                              parameters={parameterItems}
                              searchQuery={debouncedSearch}
                              keywordMatchesParameter={matchesParameter}
                            />
                          </TableCell>
                        </TableRow>
                      )}
                    </React.Fragment>
                  );
                })
              )}
            </TableBody>
          </Table>
          <div className="relative px-3 py-3">
            <div className="absolute left-3 top-1/2 flex -translate-y-1/2 items-center gap-2 text-sm">
              <span className="text-slate-400">Items per page</span>
              <select
                className="h-9 rounded-md border border-slate-300 bg-background px-3"
                value={itemsPerPage}
                onChange={(e) => {
                  const next = Number(e.target.value);
                  const params = new URLSearchParams(searchParams);
                  params.set("page_size", String(next));
                  params.set("page", "1");
                  setSearchParams(params, { replace: true });
                }}
              >
                <option value={5}>5</option>
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
            </div>
            <Pagination className="pt-0">
              <PaginationContent>
                <PaginationItem>
                  <PaginationPrevious
                    className={cn({
                      "cursor-not-allowed opacity-50": page === 1,
                    })}
                    onClick={handlePreviousPage}
                  />
                </PaginationItem>
                <PaginationItem>
                  <PaginationLink>{page}</PaginationLink>
                </PaginationItem>
                <PaginationItem>
                  <PaginationNext
                    className={cn({
                      "cursor-not-allowed opacity-50": isNextDisabled,
                    })}
                    onClick={handleNextPage}
                  />
                </PaginationItem>
              </PaginationContent>
            </Pagination>
          </div>
        </div>

        {/* Folder Dialogs */}
        <CreateFolderDialog
          open={isCreateFolderOpen}
          onOpenChange={setIsCreateFolderOpen}
        />
        <ViewAllFoldersDialog
          open={isViewAllFoldersOpen}
          onOpenChange={setIsViewAllFoldersOpen}
          selectedFolderId={selectedFolderId}
          onFolderSelect={setSelectedFolderId}
        />

        {/* Template Dialog */}
        <CreateFromTemplateDialog
          open={isTemplateDialogOpen}
          onOpenChange={setIsTemplateDialogOpen}
          onSelectTemplate={(template) => {
            const clonedWorkflow = convert({
              ...template,
              title: `${template.title} (copy)`,
            });
            createWorkflowMutation.mutate({
              ...clonedWorkflow,
              folder_id: selectedFolderId,
            });
          }}
        />

        <WorkflowTemplates />
      </div>
    </div>
  );
}

export { Workflows };
