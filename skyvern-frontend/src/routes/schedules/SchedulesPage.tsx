import { useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useDebounce } from "use-debounce";
import {
  ChevronDownIcon,
  CopyIcon,
  DotsHorizontalIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  ReloadIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Pagination,
  PaginationContent,
  PaginationItem,
  PaginationLink,
  PaginationNext,
  PaginationPrevious,
} from "@/components/ui/pagination";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { TableSearchInput } from "@/components/TableSearchInput";
import { useOrganizationSchedulesQuery } from "./useOrganizationSchedulesQuery";
import {
  useDeleteOrgScheduleMutation,
  useDisableScheduleMutation,
  useDuplicateScheduleMutation,
  useEnableScheduleMutation,
} from "./useScheduleActions";
import { cronToHumanReadable } from "@/routes/workflows/editor/panels/schedulePanel/cronUtils";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import type { OrganizationScheduleItem } from "@/routes/workflows/types/scheduleTypes";
import { CreateOrgScheduleDialog } from "./CreateOrgScheduleDialog";

type ScheduleStatus = "active" | "paused";

const STATUS_OPTIONS: Array<{ label: string; value: ScheduleStatus }> = [
  { label: "Active", value: "active" },
  { label: "Paused", value: "paused" },
];

function StatusDisplay({ enabled }: Readonly<{ enabled: boolean }>) {
  if (enabled) {
    return (
      <div className="flex items-center gap-1.5">
        <span className="size-4 text-green-400">
          <svg
            viewBox="0 0 16 16"
            fill="none"
            xmlns="http://www.w3.org/2000/svg"
          >
            <circle cx="8" cy="8" r="4" fill="currentColor" />
          </svg>
        </span>
        <span className="text-sm capitalize text-slate-300">active</span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-1.5">
      <span className="size-4 text-amber-400">
        <svg viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg">
          <circle cx="8" cy="8" r="4" fill="currentColor" />
        </svg>
      </span>
      <span className="text-sm capitalize text-slate-300">paused</span>
    </div>
  );
}

const PAGE_SIZE_OPTIONS = ["10", "25", "50"];

function SchedulesPage() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const page = Number(searchParams.get("page") || "1");
  const pageSize = Number(searchParams.get("page_size") || "10");
  const [search, setSearch] = useState("");
  const [debouncedSearch] = useDebounce(search, 500);
  const [statusFilters, setStatusFilters] = useState<ScheduleStatus[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const lastSelectedIndex = useRef<number | null>(null);
  const [deleteDialog, setDeleteDialog] = useState<{
    open: boolean;
    schedule: OrganizationScheduleItem | null;
  }>({ open: false, schedule: null });
  const [createDialogOpen, setCreateDialogOpen] = useState(false);

  const statusFilter =
    statusFilters.length === 1 ? statusFilters[0] : undefined;

  const { data, isLoading, isError, error } = useOrganizationSchedulesQuery({
    page,
    pageSize,
    statusFilter,
    search: debouncedSearch || undefined,
  });

  const enableMutation = useEnableScheduleMutation();
  const disableMutation = useDisableScheduleMutation();
  const deleteMutation = useDeleteOrgScheduleMutation();
  const duplicateMutation = useDuplicateScheduleMutation();

  const schedules = data?.schedules ?? [];
  const totalCount = data?.total_count ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalCount / pageSize));

  const allSelected =
    schedules.length > 0 &&
    schedules.every((s) => selected.has(s.workflow_schedule_id));

  function toggleSelectAll() {
    if (allSelected) {
      setSelected(new Set());
    } else {
      setSelected(new Set(schedules.map((s) => s.workflow_schedule_id)));
    }
  }

  function handleSelect(index: number, shiftKey: boolean) {
    const id = schedules[index]!.workflow_schedule_id;
    if (shiftKey && lastSelectedIndex.current !== null) {
      const start = Math.min(lastSelectedIndex.current, index);
      const end = Math.max(lastSelectedIndex.current, index);
      setSelected((prev) => {
        const next = new Set(prev);
        for (let i = start; i <= end; i++) {
          next.add(schedules[i]!.workflow_schedule_id);
        }
        return next;
      });
    } else {
      setSelected((prev) => {
        const next = new Set(prev);
        if (next.has(id)) {
          next.delete(id);
        } else {
          next.add(id);
        }
        return next;
      });
    }
    lastSelectedIndex.current = index;
  }

  function setPage(p: number) {
    const params = new URLSearchParams(searchParams);
    params.set("page", String(p));
    setSearchParams(params);
    setSelected(new Set());
  }

  function setPageSize(size: string) {
    const params = new URLSearchParams(searchParams);
    params.set("page_size", size);
    params.set("page", "1");
    setSearchParams(params);
    setSelected(new Set());
  }

  function handleDeleteConfirm() {
    if (!deleteDialog.schedule) return;
    deleteMutation.mutate(deleteDialog.schedule, {
      onSettled: () => {
        setDeleteDialog({ open: false, schedule: null });
        setSelected((prev) => {
          const next = new Set(prev);
          next.delete(deleteDialog.schedule!.workflow_schedule_id);
          return next;
        });
      },
    });
  }

  const selectedSchedules = schedules.filter((s) =>
    selected.has(s.workflow_schedule_id),
  );

  function handleBulkActivate() {
    selectedSchedules
      .filter((s) => !s.enabled)
      .forEach((s) => enableMutation.mutate(s));
    setSelected(new Set());
  }

  function handleBulkPause() {
    selectedSchedules
      .filter((s) => s.enabled)
      .forEach((s) => disableMutation.mutate(s));
    setSelected(new Set());
  }

  function handleBulkDuplicate() {
    selectedSchedules.forEach((s) => duplicateMutation.mutate(s));
    setSelected(new Set());
  }

  function handleBulkDelete() {
    selectedSchedules.forEach((s) => deleteMutation.mutate(s));
    setSelected(new Set());
  }

  const showCheckbox = schedules.length > 1;
  const columnCount = showCheckbox ? 7 : 6;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-2xl">Schedules</h1>
      </div>

      {/* Search + Filters + Create */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <TableSearchInput
            value={search}
            onChange={setSearch}
            placeholder="Search by workflow or schedule name..."
            className="w-64"
          />
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline">
                Filter by Status
                <ChevronDownIcon className="ml-2 size-4" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              {STATUS_OPTIONS.map((opt) => (
                <div
                  key={opt.value}
                  className="flex items-center gap-2 p-2 text-sm"
                >
                  <Checkbox
                    id={`schedule-status-${opt.value}`}
                    checked={statusFilters.includes(opt.value)}
                    onCheckedChange={(checked) => {
                      setStatusFilters((prev) =>
                        checked
                          ? [...prev, opt.value]
                          : prev.filter((f) => f !== opt.value),
                      );
                      setPage(1);
                    }}
                  />
                  <label htmlFor={`schedule-status-${opt.value}`}>
                    {opt.label}
                  </label>
                </div>
              ))}
              {statusFilters.length > 0 && (
                <button
                  type="button"
                  className="w-full cursor-pointer p-2 text-left text-sm text-slate-400 hover:text-slate-200"
                  onClick={() => {
                    setStatusFilters([]);
                    setPage(1);
                  }}
                >
                  Clear all
                </button>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <Button onClick={() => setCreateDialogOpen(true)}>
          <PlusIcon className="mr-1.5 size-4" />
          Create Schedule
        </Button>
      </div>

      {/* Table */}
      <div className="space-y-4">
        <div className="overflow-hidden rounded-lg border border-slate-700">
          <Table className="table-fixed">
            <TableHeader className="bg-slate-elevation2 text-slate-400 [&_tr]:border-b-0">
              <TableRow>
                {showCheckbox && (
                  <TableHead className="w-[3%]">
                    <Checkbox
                      checked={allSelected}
                      onCheckedChange={toggleSelectAll}
                    />
                  </TableHead>
                )}
                <TableHead className={showCheckbox ? "w-[28%]" : "w-[31%]"}>
                  Workflow
                </TableHead>
                <TableHead className="w-[20%]">Name</TableHead>
                <TableHead className="w-[20%]">Schedule</TableHead>
                <TableHead className="w-[17%]">Next Run</TableHead>
                <TableHead className="w-[7%]">Status</TableHead>
                <TableHead className="w-[5%]" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {isLoading && (
                <TableRow>
                  <TableCell colSpan={columnCount} className="py-8 text-center">
                    <ReloadIcon className="mx-auto size-5 animate-spin text-slate-400" />
                  </TableCell>
                </TableRow>
              )}
              {isError && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-sm text-red-400"
                  >
                    Failed to load schedules.
                    {error?.message && (
                      <span className="block text-xs text-slate-500">
                        {error.message}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              )}
              {!isLoading && !isError && schedules.length === 0 && (
                <TableRow>
                  <TableCell
                    colSpan={columnCount}
                    className="py-8 text-center text-sm text-slate-500"
                  >
                    No schedules found.
                  </TableCell>
                </TableRow>
              )}
              {schedules.map((schedule, index) => (
                <TableRow
                  key={schedule.workflow_schedule_id}
                  className="cursor-pointer select-none hover:bg-muted/50"
                  onClick={() =>
                    navigate(
                      `/schedules/${schedule.workflow_permanent_id}/${schedule.workflow_schedule_id}`,
                      { state: { workflowTitle: schedule.workflow_title } },
                    )
                  }
                >
                  {showCheckbox && (
                    <TableCell
                      onClick={(e) => {
                        e.stopPropagation();
                        handleSelect(index, e.shiftKey);
                      }}
                    >
                      <Checkbox
                        checked={selected.has(schedule.workflow_schedule_id)}
                        className="pointer-events-none"
                      />
                    </TableCell>
                  )}
                  <TableCell className="truncate font-medium">
                    {schedule.workflow_title}
                  </TableCell>
                  <TableCell className="truncate text-slate-400">
                    {schedule.name ?? "\u2014"}
                  </TableCell>
                  <TableCell className="text-slate-400">
                    {cronToHumanReadable(schedule.cron_expression)}
                  </TableCell>
                  <TableCell className="text-slate-400">
                    {schedule.next_run ? (
                      <span title={basicTimeFormat(schedule.next_run)}>
                        {basicLocalTimeFormat(schedule.next_run)}
                      </span>
                    ) : (
                      "\u2014"
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusDisplay enabled={schedule.enabled} />
                  </TableCell>
                  <TableCell onClick={(e) => e.stopPropagation()}>
                    <DropdownMenu modal={false}>
                      <DropdownMenuTrigger asChild>
                        <Button
                          variant="outline"
                          size="icon"
                          className="size-8"
                        >
                          <DotsHorizontalIcon className="size-4" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {schedule.enabled ? (
                          <DropdownMenuItem
                            onSelect={() => disableMutation.mutate(schedule)}
                          >
                            <PauseIcon className="mr-2 size-4" />
                            Pause
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem
                            onSelect={() => enableMutation.mutate(schedule)}
                          >
                            <PlayIcon className="mr-2 size-4" />
                            Activate
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem
                          onSelect={() => duplicateMutation.mutate(schedule)}
                        >
                          <CopyIcon className="mr-2 size-4" />
                          Duplicate
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          onSelect={() =>
                            setDeleteDialog({ open: true, schedule })
                          }
                          className="text-destructive"
                        >
                          <TrashIcon className="mr-2 size-4" />
                          Delete
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {/* Pagination with Items per page */}
          <div className="grid grid-cols-3 items-center px-4 py-2">
            <div className="flex items-center gap-2">
              <span className="whitespace-nowrap text-sm text-slate-400">
                Items per page
              </span>
              <Select value={String(pageSize)} onValueChange={setPageSize}>
                <SelectTrigger className="w-[65px]">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PAGE_SIZE_OPTIONS.map((size) => (
                    <SelectItem key={size} value={size}>
                      {size}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="justify-self-center">
              {totalPages > 1 && (
                <Pagination>
                  <PaginationContent>
                    <PaginationItem>
                      <PaginationPrevious
                        onClick={() => setPage(Math.max(1, page - 1))}
                        className={
                          page <= 1
                            ? "pointer-events-none opacity-50"
                            : "cursor-pointer"
                        }
                      />
                    </PaginationItem>
                    {Array.from({ length: totalPages }, (_, i) => i + 1).map(
                      (p) => (
                        <PaginationItem key={p}>
                          <PaginationLink
                            onClick={() => setPage(p)}
                            isActive={p === page}
                            className="cursor-pointer"
                          >
                            {p}
                          </PaginationLink>
                        </PaginationItem>
                      ),
                    )}
                    <PaginationItem>
                      <PaginationNext
                        onClick={() => setPage(Math.min(totalPages, page + 1))}
                        className={
                          page >= totalPages
                            ? "pointer-events-none opacity-50"
                            : "cursor-pointer"
                        }
                      />
                    </PaginationItem>
                  </PaginationContent>
                </Pagination>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Multi-select bulk action bar */}
      {selected.size > 0 && (
        <div className="fixed inset-x-0 bottom-6 mx-auto flex w-fit items-center gap-3 rounded-lg border border-slate-700 bg-slate-900 px-6 py-3 shadow-xl">
          <span className="text-sm text-slate-300">
            {selected.size} selected
          </span>
          <div className="h-6 w-px bg-slate-700" />
          <Button
            size="sm"
            className="bg-green-900 text-green-50 hover:bg-green-800"
            onClick={handleBulkActivate}
          >
            <PlayIcon className="mr-1.5 size-3.5" />
            Activate
          </Button>
          <Button
            size="sm"
            className="bg-amber-800 text-amber-50 hover:bg-amber-700"
            onClick={handleBulkPause}
          >
            <PauseIcon className="mr-1.5 size-3.5" />
            Pause
          </Button>
          <Button
            size="sm"
            className="bg-blue-800 text-blue-50 hover:bg-blue-700"
            onClick={handleBulkDuplicate}
          >
            <CopyIcon className="mr-1.5 size-3.5" />
            Duplicate
          </Button>
          <Button
            size="sm"
            className="bg-red-900 text-red-50 hover:bg-red-800"
            onClick={handleBulkDelete}
          >
            <TrashIcon className="mr-1.5 size-3.5" />
            Delete
          </Button>
        </div>
      )}

      {/* Delete confirmation dialog */}
      <Dialog
        open={deleteDialog.open}
        onOpenChange={(open) => {
          if (!open && !deleteMutation.isPending) {
            setDeleteDialog({ open: false, schedule: null });
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete Schedule</DialogTitle>
            <DialogDescription>
              Are you sure you want to delete this schedule? This action cannot
              be undone.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="secondary"
              disabled={deleteMutation.isPending}
              onClick={() => setDeleteDialog({ open: false, schedule: null })}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={handleDeleteConfirm}
            >
              {deleteMutation.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Create Schedule Dialog */}
      <CreateOrgScheduleDialog
        open={createDialogOpen}
        onOpenChange={setCreateDialogOpen}
      />
    </div>
  );
}

export { SchedulesPage };
