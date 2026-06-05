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
  TableMessageRow,
  TableRow,
} from "@/components/ui/table";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicTimeFormat, compactLocalDateTime } from "@/util/timeFormat";
import { cn } from "@/util/utils";
import {
  BookmarkFilledIcon,
  ChevronDownIcon,
  DotsHorizontalIcon,
  LightningBoltIcon,
  Pencil2Icon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
} from "@radix-ui/react-icons";
import { FolderIcon } from "@/components/icons/FolderIcon";
import { useQuery } from "@tanstack/react-query";
import React, { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
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
import { useTagKeysQuery } from "./hooks/useTagKeysQuery";
import { useWorkflowTagsBatchQuery } from "./hooks/useWorkflowTagsBatchQuery";
import { useActiveImportsPolling } from "./hooks/useActiveImportsPolling";
import { TagChipList } from "./components/tagging/TagChipList";
import { WorkflowTagFilter } from "./components/tagging/WorkflowTagFilter";
import { WorkflowTagEditor } from "./components/tagging/WorkflowTagEditor";
import {
  parseTagFilter,
  serializeTagFilter,
  type TagFilterPair,
} from "./types/tagTypes";
import { ImportWorkflowButton } from "./ImportWorkflowButton";
import { convert } from "./editor/workflowEditorUtils";
import { WorkflowApiResponse } from "./types/workflowTypes";
import { WorkflowActions } from "./WorkflowActions";
import { WorkflowTemplates } from "../discover/WorkflowTemplates";
import { Skeleton } from "@/components/ui/skeleton";
import { TableSearchInput } from "@/components/TableSearchInput";
import { ParameterDisplayInline } from "./components/ParameterDisplayInline";
import { useKeywordSearch } from "./hooks/useKeywordSearch";
import { useParameterExpansion } from "./hooks/useParameterExpansion";
import { Folder } from "./types/folderTypes";
import { defaultWorkflowRequest } from "./defaultWorkflowRequest";

// Utility function to create URL-safe folder slugs from folder names
function slugifyFolderName(name: string): string {
  return name
    .toLowerCase()
    .trim()
    .replace(/[^\w\s-]/g, "") // Remove special characters except spaces and hyphens
    .replace(/\s+/g, "-") // Replace spaces with hyphens
    .replace(/-+/g, "-") // Replace multiple hyphens with single hyphen
    .replace(/^-|-$/g, ""); // Remove leading/trailing hyphens
}

// Generate a unique slug for a folder, appending a number suffix only if there's a collision
function getUniqueSlugForFolder(folder: Folder, allFolders: Folder[]): string {
  const baseSlug = slugifyFolderName(folder.title);

  // Find all folders that would have the same base slug
  const foldersWithSameSlug = allFolders.filter(
    (f) => slugifyFolderName(f.title) === baseSlug,
  );

  // If no collision, return the base slug
  if (foldersWithSameSlug.length <= 1) {
    return baseSlug;
  }

  // Sort by created_at to ensure consistent numbering
  const sortedFolders = [...foldersWithSameSlug].sort(
    (a, b) =>
      new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
  );

  const index = sortedFolders.findIndex(
    (f) => f.folder_id === folder.folder_id,
  );

  // First folder (oldest) gets the base slug, others get numbered suffixes
  return index === 0 ? baseSlug : `${baseSlug}-${index + 1}`;
}
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

  // Folder slug from query param (e.g., /workflows?folder=my-folder-name)
  const folderSlug = searchParams.get("folder");

  // Tag filter from query param (e.g., /workflows?tags=env:prod,env:staging).
  // The backend accepts both comma-separated pairs and repeated `tags` params,
  // so read every occurrence. The serialized form is canonical (sorted) and is
  // used for both the request and the query key so cache hits are
  // order-independent.
  const tagFilterParam = searchParams.getAll("tags").join(",");
  const tagFilters = useMemo(
    () => parseTagFilter(tagFilterParam),
    [tagFilterParam],
  );
  const serializedTagFilter = serializeTagFilter(tagFilters);

  const setTagFilters = useCallback(
    (pairs: TagFilterPair[]) => {
      const params = new URLSearchParams(searchParams);
      const serialized = serializeTagFilter(pairs);
      if (serialized) {
        params.set("tags", serialized);
      } else {
        params.delete("tags");
      }
      params.set("page", "1");
      setSearchParams(params, { replace: true });
    },
    [searchParams, setSearchParams],
  );

  const [isCreateFolderOpen, setIsCreateFolderOpen] = useState(false);
  const [isViewAllFoldersOpen, setIsViewAllFoldersOpen] = useState(false);

  // Template dialog state
  const [isTemplateDialogOpen, setIsTemplateDialogOpen] = useState(false);

  // Poll for active imports
  const { activeImports, startPolling } = useActiveImportsPolling();

  // Fetch folders
  const { data: allFolders = [], isLoading: isFoldersLoading } =
    useFoldersQuery({ page_size: 10 });

  // Create a memoized map of slugs to folders to avoid O(n²) lookups
  const slugToFolderMap = useMemo(() => {
    const map = new Map<string, Folder>();
    for (const folder of allFolders) {
      const slug = getUniqueSlugForFolder(folder, allFolders);
      map.set(slug, folder);
    }
    return map;
  }, [allFolders]);

  // Folder state - derived from URL query param
  // Look up the folder by matching the slug (handles collision suffixes like my-folder-2)
  const selectedFolderId = useMemo(() => {
    if (!folderSlug || allFolders.length === 0) return null;
    const matchingFolder = slugToFolderMap.get(folderSlug);
    return matchingFolder?.folder_id ?? null;
  }, [folderSlug, allFolders.length, slugToFolderMap]);

  // Clear folder param if the folder slug is invalid (folder deleted/renamed)
  // Only validate after folders have finished loading to avoid race conditions
  useEffect(() => {
    if (
      folderSlug &&
      !selectedFolderId &&
      allFolders.length > 0 &&
      !isFoldersLoading
    ) {
      const params = new URLSearchParams(searchParams);
      params.delete("folder");
      setSearchParams(params, { replace: true });
    }
  }, [
    folderSlug,
    selectedFolderId,
    allFolders.length,
    isFoldersLoading,
    searchParams,
    setSearchParams,
  ]);

  // Update folder query param
  const setSelectedFolderId = (folderId: string | null) => {
    const params = new URLSearchParams(searchParams);
    if (folderId) {
      const folder = allFolders.find((f) => f.folder_id === folderId);
      if (folder) {
        const slug = getUniqueSlugForFolder(folder, allFolders);
        params.set("folder", slug);
        params.set("page", "1"); // Reset to page 1 when changing folder
        setSearchParams(params, { replace: true });
        return;
      }
    }
    // Remove folder filter
    params.delete("folder");
    params.set("page", "1");
    setSearchParams(params, { replace: true });
  };

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
      serializedTagFilter,
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
      if (serializedTagFilter) {
        params.append("tags", serializedTagFilter);
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
      serializedTagFilter,
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
      if (serializedTagFilter) {
        params.append("tags", serializedTagFilter);
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

  // Tag-key registry: supplies chip-hover descriptions and the filter's key list.
  // Keyed with a Map so tag keys like "constructor"/"toString" can't resolve to
  // inherited Object prototype members when looked up.
  const { data: tagKeys = [] } = useTagKeysQuery();
  const tagDescriptions = useMemo(
    () =>
      new Map(
        tagKeys.map((tagKey): [string, string | null] => [
          tagKey.key,
          tagKey.description,
        ]),
      ),
    [tagKeys],
  );

  // One batch fetch of current tags for every workflow on the page (no N+1).
  const workflowIds = useMemo(
    () => workflows.map((workflow) => workflow.workflow_permanent_id),
    [workflows],
  );
  const { data: workflowTagsMap = {} } = useWorkflowTagsBatchQuery(workflowIds);

  // Distinct values per key observed on the page, used as filter suggestions.
  // Maps avoid prototype-key collisions from user-controlled tag keys.
  const tagValueSuggestions = useMemo(() => {
    const collected = new Map<string, Set<string>>();
    for (const tags of Object.values(workflowTagsMap)) {
      for (const [key, value] of Object.entries(tags)) {
        let values = collected.get(key);
        if (!values) {
          values = new Set<string>();
          collected.set(key, values);
        }
        values.add(value);
      }
    }
    const suggestions = new Map<string, string[]>();
    for (const [key, values] of collected) {
      suggestions.set(key, [...values].sort());
    }
    return suggestions;
  }, [workflowTagsMap]);

  const { matchesParameter, isSearchActive } =
    useKeywordSearch(debouncedSearch);
  const {
    expandedRows,
    toggleExpanded: toggleParametersExpanded,
    setAutoExpandedRows,
    setManuallyExpandedRows,
  } = useParameterExpansion();

  useEffect(() => {
    if (!isSearchActive) {
      setAutoExpandedRows([]);
      setManuallyExpandedRows(new Set());
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
  }, [
    isSearchActive,
    workflows,
    matchesParameter,
    setAutoExpandedRows,
    setManuallyExpandedRows,
  ]);

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

  // Show importing agents from polling hook (only on page 1)
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
            <h1 className="text-2xl">Agents</h1>
          </div>
          <p className="text-sm leading-6 text-muted-foreground">
            Create your own complex agents by connecting web agents together.
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
                <FolderIcon className="mx-auto mb-3 h-10 w-10 text-blue-400 opacity-50" />
                <h3 className="mb-2 text-slate-900 dark:text-slate-100">
                  Organize Your Agents with Folders
                </h3>
                <p className="mb-4 text-sm text-slate-500 dark:text-slate-400">
                  Keep your agents organized by creating folders. Group related
                  agents together by project, team, or agent type for easier
                  management.
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

        {/* Agents Section */}
        <header className="flex items-center justify-between">
          <h1 className="text-xl">My Agents</h1>
          {selectedFolderId && (
            <Button
              variant="link"
              size="sm"
              className="h-auto p-0 text-blue-600 dark:text-blue-400"
              onClick={() => setSelectedFolderId(null)}
            >
              View all agents
            </Button>
          )}
        </header>
        <div className="flex justify-between">
          <div className="flex items-center gap-3">
            <TableSearchInput
              value={search}
              onChange={(value) => {
                setSearch(value);
                setParamPatch({ page: "1" });
              }}
              placeholder="Search by title or input..."
              className="w-48 lg:w-72"
            />
            <WorkflowTagFilter
              tagKeys={tagKeys}
              value={tagFilters}
              onChange={setTagFilters}
              valueSuggestions={tagValueSuggestions}
            />
          </div>
          <div className="flex items-center gap-4">
            <Link
              to="/discover"
              className="text-sm text-muted-foreground hover:text-foreground"
            >
              Or start from a description →
            </Link>
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
                      ...defaultWorkflowRequest,
                      folder_id: selectedFolderId,
                      _via: "blank",
                    });
                  }}
                >
                  <PlusIcon className="mr-2 h-4 w-4" />
                  Blank Agent
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
        <div className="overflow-hidden rounded-lg border border-border">
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="w-[25%]">ID</TableHead>
                <TableHead className="w-[30%]">Title</TableHead>
                <TableHead className="w-[15%]">Folder</TableHead>
                <TableHead className="w-[15%]">Created At</TableHead>
                <TableHead className="w-[15%] text-right">Actions</TableHead>
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
                <TableMessageRow colSpan={5}>No agents found</TableMessageRow>
              ) : (
                displayWorkflows?.map((workflow) => {
                  const parameterItems = workflow.workflow_definition.parameters
                    .filter((p) => p.parameter_type !== "output")
                    .map((param) => ({
                      key: param.key,
                      value:
                        param.parameter_type === "workflow"
                          ? (param.default_value ?? "")
                          : "",
                      description: param.description ?? null,
                    }));
                  const hasParameters = parameterItems.length > 0;
                  const isExpanded = expandedRows.has(
                    workflow.workflow_permanent_id,
                  );
                  // Check if this is an importing agent
                  const isUploading = workflow.status === "importing";
                  const workflowTags =
                    workflowTagsMap[workflow.workflow_permanent_id];

                  return (
                    <React.Fragment key={workflow.workflow_permanent_id}>
                      {/* Main workflow row */}
                      {isUploading ? (
                        <TableRow className="opacity-70">
                          <TableCell colSpan={2}>
                            <div className="flex min-w-0 items-center gap-2">
                              <ReloadIcon className="h-4 w-4 shrink-0 animate-spin text-blue-400" />
                              <span className="truncate" title={workflow.title}>
                                {workflow.title}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <span className="text-muted-foreground">-</span>
                          </TableCell>
                          <TableCell className="text-muted-foreground">
                            {compactLocalDateTime(workflow.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-0.5">
                              <Button size="icon" variant="ghost" disabled>
                                <FolderIcon className="h-4 w-4" />
                              </Button>
                              <Button size="icon" variant="ghost" disabled>
                                <Pencil2Icon className="h-4 w-4" />
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
                            <div
                              className="truncate font-mono text-xs text-muted-foreground"
                              title={workflow.workflow_permanent_id}
                            >
                              <HighlightText
                                text={workflow.workflow_permanent_id}
                                query={debouncedSearch}
                              />
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
                            <div className="flex min-w-0 flex-col gap-1">
                              <div className="flex min-w-0 items-center gap-2">
                                <span
                                  className="truncate"
                                  title={workflow.title}
                                >
                                  <HighlightText
                                    text={workflow.title}
                                    query={debouncedSearch}
                                  />
                                </span>
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
                              {workflowTags &&
                              Object.keys(workflowTags).length > 0 ? (
                                <TagChipList
                                  tags={workflowTags}
                                  descriptions={tagDescriptions}
                                />
                              ) : null}
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
                              <div className="flex min-w-0 items-center gap-1.5">
                                <FolderIcon className="h-3.5 w-3.5 shrink-0 text-blue-400" />
                                <span
                                  className="truncate text-sm"
                                  title={
                                    foldersMap.get(workflow.folder_id)?.title ||
                                    workflow.folder_id
                                  }
                                >
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
                              <span className="text-muted-foreground">-</span>
                            )}
                          </TableCell>
                          <TableCell
                            onClick={(event) => {
                              handleRowClick(
                                event,
                                workflow.workflow_permanent_id,
                              );
                            }}
                            className="text-muted-foreground"
                            title={basicTimeFormat(workflow.created_at)}
                          >
                            {compactLocalDateTime(workflow.created_at)}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-0.5">
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
                              <WorkflowTagEditor
                                workflowPermanentId={
                                  workflow.workflow_permanent_id
                                }
                                tags={workflowTags ?? {}}
                                tagKeys={tagKeys}
                                valueSuggestions={tagValueSuggestions}
                              />
                              <TooltipProvider>
                                <Tooltip>
                                  <TooltipTrigger asChild>
                                    <Button
                                      size="icon"
                                      variant="ghost"
                                      className="text-muted-foreground hover:text-foreground"
                                      onClick={(event) => {
                                        handleIconClick(
                                          event,
                                          `/workflows/${workflow.workflow_permanent_id}/build`,
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
                                      variant="ghost"
                                      className="text-cta hover:text-cta"
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
                              <WorkflowActions
                                workflow={workflow}
                                hasParameters={hasParameters}
                                parametersExpanded={isExpanded}
                                onToggleParameters={() =>
                                  toggleParametersExpanded(
                                    workflow.workflow_permanent_id,
                                  )
                                }
                              />
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
