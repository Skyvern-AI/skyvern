import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  DotsHorizontalIcon,
  FileIcon,
  LightningBoltIcon,
  MagnifyingGlassIcon,
  MixerHorizontalIcon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { useQuery } from "@tanstack/react-query";
import React, { useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import { NarrativeCard } from "./components/header/NarrativeCard";
import { FolderCard } from "./components/FolderCard";
import { CreateFolderDialog } from "./components/CreateFolderDialog";
import { ViewAllFoldersDialog } from "./components/ViewAllFoldersDialog";
import { WorkflowFolderSelector } from "./components/WorkflowFolderSelector";
import { HighlightText } from "./components/HighlightText";
import { useCreateWorkflowMutation } from "./hooks/useCreateWorkflowMutation";
import { useFoldersQuery } from "./hooks/useFoldersQuery";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowCreateYAMLRequest } from "./types/workflowYamlTypes";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";

const emptyWorkflowRequest: WorkflowCreateYAMLRequest = {
  title: "New Workflow",
  description: "",
  ai_fallback: true,
  run_with: "agent",
  workflow_definition: {
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
  const [debouncedSearch] = useDebounce(search, 500);
  const page = searchParams.get("page") ? Number(searchParams.get("page")) : 1;
  const itemsPerPage = searchParams.get("page_size")
    ? Number(searchParams.get("page_size"))
    : 10;

  // Folder state
  const [selectedFolderId, setSelectedFolderId] = useState<string | null>(null);
  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [isViewAllFoldersOpen, setIsViewAllFoldersOpen] = useState(false);

  // Parameter expansion state
  const [manuallyExpandedRows, setManuallyExpandedRows] = useState<Set<string>>(
    new Set()
  );

  // Track uploading workflows - persist to sessionStorage
  const [uploadingWorkflows, setUploadingWorkflows] = useState<Set<string>>(() => {
    const stored = sessionStorage.getItem("skyvern.uploadingWorkflows");
    return stored ? new Set(JSON.parse(stored)) : new Set();
  });
  const [placeholderWorkflows, setPlaceholderWorkflows] = useState<
    Map<string, WorkflowApiResponse>
  >(() => {
    const stored = sessionStorage.getItem("skyvern.placeholderWorkflows");
    return stored ? new Map(Object.entries(JSON.parse(stored))) : new Map();
  });

  // Persist uploading state to sessionStorage whenever it changes
  React.useEffect(() => {
    if (uploadingWorkflows.size > 0) {
      sessionStorage.setItem(
        "skyvern.uploadingWorkflows",
        JSON.stringify(Array.from(uploadingWorkflows))
      );
    }
  }, [uploadingWorkflows]);

  React.useEffect(() => {
    if (placeholderWorkflows.size > 0) {
      sessionStorage.setItem(
        "skyvern.placeholderWorkflows",
        JSON.stringify(Object.fromEntries(placeholderWorkflows))
      );
    }
  }, [placeholderWorkflows]);

  // Fetch folders
  const { data: allFolders = [] } = useFoldersQuery({ page_size: 10 });

  // Sort folders by modified date (most recent first) and get top 5
  const recentFolders = useMemo(() => {
    return [...allFolders]
      .sort(
        (a, b) =>
          new Date(b.modified_at).getTime() - new Date(a.modified_at).getTime()
      )
      .slice(0, 5);
  }, [allFolders]);

  const { data: workflows = [], isLoading } = useQuery<
    Array<WorkflowApiResponse>
  >({
    queryKey: ["workflows", debouncedSearch, page, itemsPerPage, selectedFolderId],
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
  });

  const { data: nextPageWorkflows } = useQuery<Array<WorkflowApiResponse>>({
    queryKey: ["workflows", debouncedSearch, page + 1, itemsPerPage, selectedFolderId],
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
    isLoading || !nextPageWorkflows || nextPageWorkflows.length === 0;

  // Auto-expand rows when parameters match search
  const autoExpandedRows = useMemo(() => {
    if (!debouncedSearch.trim()) return new Set<string>();

    const expanded = new Set<string>();
    const lowerQuery = debouncedSearch.toLowerCase();

    workflows.forEach((workflow) => {
      const hasParameterMatch = workflow.workflow_definition.parameters?.some(
        (param) => {
          const keyMatch = param.key?.toLowerCase().includes(lowerQuery);
          const descMatch = param.description?.toLowerCase().includes(lowerQuery);
          const valueMatch =
            param.parameter_type === "workflow" &&
            param.default_value &&
            String(param.default_value).toLowerCase().includes(lowerQuery);
          return keyMatch || descMatch || valueMatch;
        }
      );

      if (hasParameterMatch) {
        expanded.add(workflow.workflow_permanent_id);
      }
    });

    return expanded;
  }, [workflows, debouncedSearch]);

  // Combine manual and auto-expanded rows
  const expandedRows = useMemo(() => {
    return new Set([...manuallyExpandedRows, ...autoExpandedRows]);
  }, [manuallyExpandedRows, autoExpandedRows]);

  const toggleParametersExpanded = (workflowId: string) => {
    const newExpanded = new Set(manuallyExpandedRows);
    if (newExpanded.has(workflowId)) {
      newExpanded.delete(workflowId);
    } else {
      newExpanded.add(workflowId);
    }
    setManuallyExpandedRows(newExpanded);
  };

  // Check if a specific parameter matches the search
  const parameterMatchesSearch = (param: any): boolean => {
    if (!debouncedSearch.trim()) return false;
    const lowerQuery = debouncedSearch.toLowerCase();

    const keyMatch = param.key?.toLowerCase().includes(lowerQuery);
    const descMatch = param.description?.toLowerCase().includes(lowerQuery);
    const valueMatch =
      param.parameter_type === "workflow" &&
      param.default_value &&
      String(param.default_value).toLowerCase().includes(lowerQuery);

    return keyMatch || descMatch || valueMatch;
  };

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

  // Import workflow handlers
  const handleImportStart = (tempId: string, fileName: string) => {
    const placeholderWorkflow: WorkflowApiResponse = {
      workflow_id: tempId,
      workflow_permanent_id: tempId,
      organization_id: "",
      is_saved_task: false,
      title: `Importing ${fileName}...`,
      version: 1,
      description: "",
      workflow_definition: { parameters: [], blocks: [] },
      proxy_location: null,
      webhook_callback_url: null,
      extra_http_headers: null,
      persist_browser_session: false,
      model: null,
      totp_verification_url: null,
      totp_identifier: null,
      max_screenshot_scrolls: null,
      status: null,
      created_at: new Date().toISOString(),
      modified_at: new Date().toISOString(),
      deleted_at: null,
      run_with: null,
      cache_key: null,
      ai_fallback: null,
      run_sequentially: null,
      sequential_key: null,
      folder_id: null,
    };

    setUploadingWorkflows((prev) => new Set(prev).add(tempId));
    setPlaceholderWorkflows((prev) => new Map(prev).set(tempId, placeholderWorkflow));
  };

  const handleImportComplete = (tempId: string) => {
    setUploadingWorkflows((prev) => {
      const next = new Set(prev);
      next.delete(tempId);
      // Clean up sessionStorage if this was the last upload
      if (next.size === 0) {
        sessionStorage.removeItem("skyvern.uploadingWorkflows");
      }
      return next;
    });
    setPlaceholderWorkflows((prev) => {
      const next = new Map(prev);
      next.delete(tempId);
      // Clean up sessionStorage if this was the last placeholder
      if (next.size === 0) {
        sessionStorage.removeItem("skyvern.placeholderWorkflows");
      }
      return next;
    });
  };

  const handleImportError = (tempId: string) => {
    setUploadingWorkflows((prev) => {
      const next = new Set(prev);
      next.delete(tempId);
      // Clean up sessionStorage if this was the last upload
      if (next.size === 0) {
        sessionStorage.removeItem("skyvern.uploadingWorkflows");
      }
      return next;
    });
    setPlaceholderWorkflows((prev) => {
      const next = new Map(prev);
      next.delete(tempId);
      // Clean up sessionStorage if this was the last placeholder
      if (next.size === 0) {
        sessionStorage.removeItem("skyvern.placeholderWorkflows");
      }
      return next;
    });
  };

  // Merge placeholder workflows with real workflows (only on page 1)
  const displayWorkflows = useMemo(() => {
    if (page === 1) {
      const placeholders = Array.from(placeholderWorkflows.values());
      return [...placeholders, ...workflows];
    }
    return workflows;
  }, [placeholderWorkflows, workflows, page]);

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

          {recentFolders.length > 0 && (
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
                        : folder.folder_id
                    )
                  }
                />
              ))}
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
          <div className="relative">
            <div className="absolute left-0 top-0 flex size-9 items-center justify-center">
              <MagnifyingGlassIcon className="size-6" />
            </div>
            <Input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
                setParamPatch({ page: "1" });
              }}
              placeholder="Search by title or parameter..."
              className="w-48 pl-9 lg:w-72"
            />
          </div>
          <div className="flex gap-4">
            <ImportWorkflowButton
              onImportStart={handleImportStart}
              onImportComplete={handleImportComplete}
              onImportError={handleImportError}
            />
            <Button
              disabled={createWorkflowMutation.isPending}
              onClick={() => {
                createWorkflowMutation.mutate(emptyWorkflowRequest);
              }}
            >
              {createWorkflowMutation.isPending ? (
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <PlusIcon className="mr-2 h-4 w-4" />
              )}
              Create
            </Button>
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
              {isLoading ? (
                <TableRow>
                  <TableCell colSpan={5}>Loading...</TableCell>
                </TableRow>
              ) : displayWorkflows?.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={5}>No workflows found</TableCell>
                </TableRow>
              ) : (
                displayWorkflows?.map((workflow) => {
                  const hasParameters =
                    workflow.workflow_definition.parameters.filter(
                      (p) => p.parameter_type !== "output"
                    ).length > 0;
                  const isExpanded = expandedRows.has(
                    workflow.workflow_permanent_id
                  );
                  const isUploading = uploadingWorkflows.has(
                    workflow.workflow_permanent_id
                  );

                  return (
                    <>
                      {/* Main workflow row */}
                      {isUploading ? (
                        <TableRow
                          key={workflow.workflow_permanent_id}
                          className="opacity-70"
                        >
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
                        <TableRow
                          key={workflow.workflow_permanent_id}
                          className="cursor-pointer"
                        >
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id
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
                                workflow.workflow_permanent_id
                              );
                            }}
                          >
                            <HighlightText
                              text={workflow.title}
                              query={debouncedSearch}
                            />
                          </TableCell>
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id
                              );
                            }}
                          >
                            {workflow.folder_id ? (
                              <div className="flex items-center gap-1.5">
                                <FileIcon className="h-3.5 w-3.5 text-blue-400" />
                                <span className="text-sm">
                                  <HighlightText
                                    text={
                                      allFolders.find(
                                        (f) => f.folder_id === workflow.folder_id
                                      )?.title || workflow.folder_id
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
                                workflow.workflow_permanent_id
                              );
                            }}
                            title={basicTimeFormat(workflow.created_at)}
                          >
                            {basicLocalTimeFormat(workflow.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-2">
                              <WorkflowFolderSelector
                                workflowId={workflow.workflow_permanent_id}
                                currentFolderId={workflow.folder_id}
                              />
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="outline"
                                      onClick={() =>
                                        toggleParametersExpanded(
                                          workflow.workflow_permanent_id
                                        )
                                      }
                                      disabled={!hasParameters}
                                      className={cn(
                                        isExpanded && "text-blue-400"
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
                                          `/workflows/${workflow.workflow_permanent_id}/run`
                                        );
                                      }}
                                    >
                                      <PlayIcon className="h-4 w-4" />
                                    </Button>
                                  </TooltipTrigger>
                                  <TooltipContent>Create New Run</TooltipContent>
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
                            <div className="ml-8 space-y-2 py-4">
                              <div className="mb-3 text-sm font-medium">
                                Parameters
                              </div>
                              <div className="space-y-2">
                                {workflow.workflow_definition.parameters
                                  .filter((p) => p.parameter_type !== "output")
                                  .map((param, idx) => {
                                    const matchesParam =
                                      parameterMatchesSearch(param);

                                    return (
                                      <div
                                        key={idx}
                                        className={cn(
                                          "grid grid-cols-[140px_1fr_2fr] gap-4 rounded border bg-white p-3 text-sm dark:border-slate-800 dark:bg-slate-900",
                                          matchesParam &&
                                            "shadow-[0_0_15px_rgba(59,130,246,0.3)] ring-2 ring-blue-500/50"
                                        )}
                                      >
                                        <div className="font-medium text-blue-600 dark:text-blue-400">
                                          <HighlightText
                                            text={param.key}
                                            query={debouncedSearch}
                                          />
                                        </div>
                                        <div className="truncate">
                                          {param.parameter_type === "workflow" &&
                                          param.default_value ? (
                                            <HighlightText
                                              text={String(param.default_value)}
                                              query={debouncedSearch}
                                            />
                                          ) : (
                                            <span className="text-slate-400">
                                              -
                                            </span>
                                          )}
                                        </div>
                                        <div className="text-slate-500">
                                          {param.description ? (
                                            <HighlightText
                                              text={param.description}
                                              query={debouncedSearch}
                                            />
                                          ) : (
                                            <span className="text-slate-400">
                                              No description
                                            </span>
                                          )}
                                        </div>
                                      </div>
                                    );
                                  })}
                              </div>
                            </div>
                          </TableCell>
                        </TableRow>
                      )}
                    </>
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

        <WorkflowTemplates />
      </div>
    </div>
  );
}

export { Workflows };
