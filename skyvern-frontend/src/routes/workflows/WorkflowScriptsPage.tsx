import { getClient } from "@/api/AxiosClient";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
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
import { toast } from "@/components/ui/use-toast";
import { useCredentialGetter } from "@/hooks/useCredentialGetter";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  DrawingPinFilledIcon,
  DrawingPinIcon,
  FileTextIcon,
  Pencil2Icon,
  PlayIcon,
  ReloadIcon,
  TrashIcon,
} from "@radix-ui/react-icons";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { type AxiosError } from "axios";
import { Fragment, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { usePinScriptMutation } from "./hooks/usePinScriptMutation";
import { useScriptVersionsQuery } from "./hooks/useScriptVersionsQuery";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowScriptsQuery } from "./hooks/useWorkflowScriptsQuery";
import { WorkflowActions } from "./WorkflowActions";
import type { WorkflowScriptSummary } from "./types/scriptTypes";

const TABLE_COL_COUNT = 6;

function PinButton({
  workflowPermanentId,
  script,
}: {
  workflowPermanentId: string;
  script: WorkflowScriptSummary;
}) {
  const pinMutation = usePinScriptMutation({ workflowPermanentId });

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className={`size-8 ${
              script.is_pinned
                ? "text-amber-500 hover:text-amber-400"
                : "text-muted-foreground hover:text-foreground"
            }`}
            disabled={pinMutation.isPending}
            onClick={(e) => {
              e.stopPropagation();
              pinMutation.mutate({
                cacheKeyValue: script.cache_key_value,
                pin: !script.is_pinned,
              });
            }}
          >
            {script.is_pinned ? (
              <DrawingPinFilledIcon className="size-4" />
            ) : (
              <DrawingPinIcon className="size-4" />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">
          {script.is_pinned
            ? "Unpin script (allow auto-updates)"
            : "Pin script (prevent auto-updates)"}
        </TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}

function DeleteScriptButton({
  workflowPermanentId,
  script,
}: {
  workflowPermanentId: string;
  script: WorkflowScriptSummary;
}) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const deleteMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.delete(
        `/scripts/${workflowPermanentId}/value?cache-key-value=${encodeURIComponent(script.cache_key_value)}`,
      );
    },
    onSuccess: () => {
      setOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["workflow-scripts", workflowPermanentId],
      });
      toast({ title: "Script deleted", variant: "success" });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to delete script",
        description: error.message,
      });
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="size-8 text-muted-foreground hover:text-destructive"
          onClick={(e) => e.stopPropagation()}
        >
          <TrashIcon className="size-4" />
        </Button>
      </DialogTrigger>
      <DialogContent onClick={(e) => e.stopPropagation()}>
        <DialogHeader>
          <DialogTitle>Delete script?</DialogTitle>
          <DialogDescription>
            This will delete the cached script for{" "}
            <span className="font-mono font-semibold text-primary">
              {script.cache_key_value}
            </span>
            . The script will be regenerated on the next run.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
          >
            {deleteMutation.isPending && (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            )}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ClearAllScriptsButton({
  workflowPermanentId,
  disabled,
}: {
  workflowPermanentId: string;
  disabled: boolean;
}) {
  const credentialGetter = useCredentialGetter();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);

  const clearMutation = useMutation({
    mutationFn: async () => {
      const client = await getClient(credentialGetter, "sans-api-v1");
      return client.delete(`/scripts/${workflowPermanentId}/cache`);
    },
    onSuccess: () => {
      setOpen(false);
      queryClient.invalidateQueries({
        queryKey: ["workflow-scripts", workflowPermanentId],
      });
      toast({ title: "All scripts cleared", variant: "success" });
    },
    onError: (error: AxiosError) => {
      toast({
        variant: "destructive",
        title: "Failed to clear scripts",
        description: error.message,
      });
    },
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="destructive" size="sm" disabled={disabled}>
          <TrashIcon className="mr-2 size-4" />
          Clear All Scripts
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Clear all scripts?</DialogTitle>
          <DialogDescription>
            This will delete all cached scripts for this workflow. Scripts will
            be regenerated on the next run. This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => clearMutation.mutate()}
            disabled={clearMutation.isPending}
          >
            {clearMutation.isPending && (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            )}
            Clear All
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ScriptsTableRows({
  isLoading,
  isError,
  scripts,
  workflowPermanentId,
}: {
  isLoading: boolean;
  isError: boolean;
  scripts: WorkflowScriptSummary[];
  workflowPermanentId: string;
}) {
  if (isLoading) {
    return (
      <TableRow>
        <TableCell colSpan={TABLE_COL_COUNT}>
          <div className="space-y-2 py-2">
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
            <Skeleton className="h-6 w-full" />
          </div>
        </TableCell>
      </TableRow>
    );
  }

  if (isError) {
    return (
      <TableRow>
        <TableCell colSpan={TABLE_COL_COUNT}>
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <p className="text-sm text-red-500">
              Failed to load scripts. Please try again.
            </p>
          </div>
        </TableCell>
      </TableRow>
    );
  }

  if (scripts.length === 0) {
    return (
      <TableRow>
        <TableCell colSpan={TABLE_COL_COUNT}>
          <div className="flex flex-col items-center gap-2 py-12 text-center">
            <FileTextIcon className="size-8 text-slate-400" />
            <p className="text-sm text-slate-500">
              No scripts yet. Scripts are created automatically when this
              workflow runs with Code mode enabled.
            </p>
          </div>
        </TableCell>
      </TableRow>
    );
  }

  return scripts.map((script) => (
    <ScriptRow
      key={script.script_id}
      script={script}
      workflowPermanentId={workflowPermanentId}
    />
  ));
}

function ScriptRow({
  script,
  workflowPermanentId,
}: {
  script: WorkflowScriptSummary;
  workflowPermanentId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const navigate = useNavigate();
  const hasRevisions = script.version_count > 1;

  const { data: versions, isLoading: versionsLoading } = useScriptVersionsQuery(
    {
      scriptId: expanded ? script.script_id : undefined,
    },
  );

  // Group versions by run_id — each sub-row = one run that modified the script.
  // versions is ordered DESC (newest first).
  const runGroups: {
    run_id: string | null;
    count: number;
    latest_at: string;
    latest_version: number;
    is_initial: boolean;
  }[] = [];
  if (versions?.versions) {
    const vList = [...versions.versions].reverse(); // chronological order
    const seen = new Map<string, number>();
    for (const v of vList) {
      const key = v.run_id ?? `__none_${v.version}`;
      const existing = seen.get(key);
      if (existing !== undefined && runGroups[existing]) {
        runGroups[existing].count += 1;
        runGroups[existing].latest_version = v.version;
      } else {
        seen.set(key, runGroups.length);
        runGroups.push({
          run_id: v.run_id,
          count: 1,
          latest_at: v.created_at,
          latest_version: v.version,
          is_initial: runGroups.length === 0,
        });
      }
    }
    runGroups.reverse(); // newest first for display
  }

  return (
    <Fragment>
      <TableRow
        className="cursor-pointer"
        onClick={() => {
          if (hasRevisions) {
            setExpanded(!expanded);
          } else {
            navigate(
              `/workflows/${workflowPermanentId}/scripts/${script.script_id}`,
            );
          }
        }}
      >
        <TableCell className="w-8">
          {hasRevisions ? (
            expanded ? (
              <ChevronDownIcon className="size-4 text-muted-foreground" />
            ) : (
              <ChevronRightIcon className="size-4 text-muted-foreground" />
            )
          ) : null}
        </TableCell>
        <TableCell>
          <div className="flex items-center gap-2">
            <PinButton
              workflowPermanentId={workflowPermanentId}
              script={script}
            />
            <div className="flex flex-col gap-0.5">
              <Link
                to={`/workflows/${workflowPermanentId}/scripts/${script.script_id}`}
                className="font-mono text-sm text-blue-400 hover:underline"
                onClick={(e) => e.stopPropagation()}
              >
                {script.cache_key_value || "(default)"}
              </Link>
              <span className="font-mono text-xs text-muted-foreground">
                {script.script_id}
              </span>
            </div>
          </div>
        </TableCell>
        <TableCell>{script.version_count}</TableCell>
        <TableCell>{script.total_runs}</TableCell>
        <TableCell title={basicTimeFormat(script.modified_at)}>
          {basicLocalTimeFormat(script.modified_at)}
        </TableCell>
        <TableCell>
          <DeleteScriptButton
            workflowPermanentId={workflowPermanentId}
            script={script}
          />
        </TableCell>
      </TableRow>
      {expanded && (
        <>
          {versionsLoading ? (
            <TableRow>
              <TableCell colSpan={TABLE_COL_COUNT}>
                <Skeleton className="mx-8 h-5 w-3/4" />
              </TableCell>
            </TableRow>
          ) : (
            runGroups.map((group) => (
              <TableRow
                key={group.run_id ?? `no-run-v${group.latest_version}`}
                className="cursor-pointer bg-muted/30 hover:bg-muted/50"
                onClick={() =>
                  navigate(
                    `/workflows/${workflowPermanentId}/scripts/${script.script_id}?version=${group.latest_version}`,
                  )
                }
              >
                <TableCell />
                <TableCell className="pl-8 text-sm">
                  {group.run_id ? (
                    <Link
                      to={`/workflows/${workflowPermanentId}/${group.run_id}/code`}
                      className="font-mono text-xs text-blue-400 hover:underline"
                      onClick={(e) => e.stopPropagation()}
                    >
                      {group.run_id}
                    </Link>
                  ) : (
                    <span className="text-xs text-muted-foreground">-</span>
                  )}
                </TableCell>
                <TableCell className="text-xs">
                  {group.is_initial ? (
                    <span className="text-muted-foreground">
                      Initial script
                    </span>
                  ) : (
                    <>
                      {group.count}{" "}
                      <span className="text-muted-foreground">
                        {group.count === 1 ? "correction" : "corrections"}
                      </span>
                    </>
                  )}
                </TableCell>
                <TableCell />
                <TableCell
                  className="text-xs"
                  title={basicTimeFormat(group.latest_at)}
                >
                  {basicLocalTimeFormat(group.latest_at)}
                </TableCell>
                <TableCell />
              </TableRow>
            ))
          )}
        </>
      )}
    </Fragment>
  );
}

function WorkflowScriptsPage() {
  const { workflowPermanentId } = useParams();
  const navigate = useNavigate();

  const { data: workflow, isLoading: workflowIsLoading } = useWorkflowQuery({
    workflowPermanentId,
  });

  const {
    data: scriptsData,
    isLoading: scriptsLoading,
    isError,
  } = useWorkflowScriptsQuery({
    workflowPermanentId,
  });

  const scripts = scriptsData?.scripts ?? [];

  if (!workflowPermanentId) {
    return null;
  }

  return (
    <div className="space-y-8">
      <header className="flex justify-between">
        <div className="flex flex-col gap-2">
          {workflowIsLoading ? (
            <>
              <Skeleton className="h-7 w-56" />
              <Skeleton className="h-7 w-56" />
            </>
          ) : (
            <>
              <Link
                to={`/workflows/${workflowPermanentId}/runs`}
                className="text-lg font-semibold hover:text-blue-400 hover:underline"
              >
                {workflow?.title}
              </Link>
              <h2 className="text-sm text-muted-foreground">
                {workflowPermanentId}
              </h2>
            </>
          )}
        </div>
        <div className="flex gap-2">
          {workflow && (
            <WorkflowActions
              workflow={workflow}
              onSuccessfullyDeleted={() => navigate("/workflows")}
            />
          )}
          <Button asChild variant="secondary">
            <Link to={`/workflows/${workflowPermanentId}/build`}>
              <Pencil2Icon className="mr-2 size-4" />
              Edit
            </Link>
          </Button>
          <Button asChild>
            <Link to={`/workflows/${workflowPermanentId}/run`}>
              <PlayIcon className="mr-2 size-4" />
              Run
            </Link>
          </Button>
        </div>
      </header>
      <div className="space-y-4">
        <header className="flex items-center justify-between">
          <h1 className="text-2xl">Scripts</h1>
          <div className="flex gap-2">
            <ClearAllScriptsButton
              workflowPermanentId={workflowPermanentId}
              disabled={scripts.length === 0}
            />
            <Button asChild variant="outline" size="sm">
              <Link to={`/workflows/${workflowPermanentId}/runs`}>
                View Runs
              </Link>
            </Button>
          </div>
        </header>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8" />
                <TableHead>Cache Key Value</TableHead>
                <TableHead>Total Revisions</TableHead>
                <TableHead>Runs</TableHead>
                <TableHead>Last Updated</TableHead>
                <TableHead className="w-12" />
              </TableRow>
            </TableHeader>
            <TableBody>
              <ScriptsTableRows
                isLoading={scriptsLoading}
                isError={isError}
                scripts={scripts}
                workflowPermanentId={workflowPermanentId}
              />
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}

export { WorkflowScriptsPage };
