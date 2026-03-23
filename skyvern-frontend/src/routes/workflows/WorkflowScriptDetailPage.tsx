import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import {
  ArrowLeftIcon,
  DrawingPinFilledIcon,
  DrawingPinIcon,
} from "@radix-ui/react-icons";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { CodeEditor } from "./components/CodeEditor";
import { usePinScriptMutation } from "./hooks/usePinScriptMutation";
import { useScriptRunsQuery } from "./hooks/useScriptRunsQuery";
import { useScriptVersionCodeQuery } from "./hooks/useScriptVersionCodeQuery";
import { useScriptVersionsQuery } from "./hooks/useScriptVersionsQuery";
import { useWorkflowScriptsQuery } from "./hooks/useWorkflowScriptsQuery";
import { ScriptFixInput } from "./workflowRun/ScriptFixInput";

const statusVariant: Record<string, "default" | "secondary" | "destructive"> = {
  completed: "default",
  running: "secondary",
  created: "secondary",
  queued: "secondary",
  failed: "destructive",
  terminated: "destructive",
  canceled: "secondary",
  timed_out: "destructive",
};

function StatusDistribution({
  statusCounts,
  totalCount,
}: {
  statusCounts: Record<string, number>;
  totalCount: number;
}) {
  if (totalCount === 0) return null;

  const statusColors: Record<string, string> = {
    completed: "bg-green-500",
    running: "bg-blue-500",
    failed: "bg-red-500",
    terminated: "bg-orange-500",
    canceled: "bg-slate-400",
    timed_out: "bg-yellow-500",
    created: "bg-slate-300",
    queued: "bg-slate-300",
  };

  return (
    <div className="space-y-2">
      <div className="flex h-3 w-full overflow-hidden rounded-full">
        {Object.entries(statusCounts).map(([status, count]) => (
          <div
            key={status}
            className={statusColors[status] ?? "bg-slate-300"}
            style={{ width: `${(count / totalCount) * 100}%` }}
            title={`${status}: ${count} (${Math.round((count / totalCount) * 100)}%)`}
          />
        ))}
      </div>
      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
        {Object.entries(statusCounts).map(([status, count]) => (
          <div key={status} className="flex items-center gap-1.5">
            <div
              className={`size-2.5 rounded-full ${statusColors[status] ?? "bg-slate-300"}`}
            />
            <span>
              {status}: {count} ({Math.round((count / totalCount) * 100)}%)
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function WorkflowScriptDetailPage() {
  const { workflowPermanentId, scriptId } = useParams();
  const [searchParams] = useSearchParams();
  const requestedVersion = searchParams.get("version");

  const { data: versions, isLoading: versionsLoading } = useScriptVersionsQuery(
    { scriptId },
  );

  const latestVersion = versions?.versions?.[0]?.version;
  // If a specific version was requested via query param, use it; otherwise latest
  const activeVersion =
    requestedVersion != null ? Number(requestedVersion) : latestVersion;
  const isLatest = activeVersion === latestVersion;

  const { data: codeData, isLoading: codeLoading } = useScriptVersionCodeQuery({
    scriptId,
    version: activeVersion,
  });

  const { data: runsData, isLoading: runsLoading } = useScriptRunsQuery({
    scriptId,
    pageSize: 50,
    version: activeVersion,
  });

  const { data: scriptsData } = useWorkflowScriptsQuery({
    workflowPermanentId,
  });

  const currentScript = scriptsData?.scripts?.find(
    (s) => s.script_id === scriptId,
  );
  const isPinned = currentScript?.is_pinned ?? false;

  const pinMutation = usePinScriptMutation({
    workflowPermanentId: workflowPermanentId ?? "",
  });

  const mainScript = codeData?.main_script ?? "";
  const activeVersionInfo = versions?.versions?.find(
    (v) => v.version === activeVersion,
  );
  const newerCount = versions?.versions
    ? versions.versions.filter((v) => v.version > (activeVersion ?? 0)).length
    : 0;
  const runs = runsData?.runs ?? [];
  const statusCounts = runsData?.status_counts ?? {};
  const totalCount = runsData?.total_count ?? 0;
  const successRate =
    totalCount > 0 ? (statusCounts["completed"] ?? 0) / totalCount : null;
  const MAX_RUNS_SHOWN = 50;

  if (!workflowPermanentId || !scriptId) return null;

  return (
    <div className="space-y-8">
      <header className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button asChild variant="ghost" size="icon">
            <Link to={`/workflows/${workflowPermanentId}/scripts`}>
              <ArrowLeftIcon className="size-5" />
            </Link>
          </Button>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold">Script Detail</h1>
              {currentScript && isPinned && (
                <Badge
                  variant="secondary"
                  className="gap-1 border-amber-500/30 bg-amber-500/10 text-amber-500"
                >
                  <DrawingPinFilledIcon className="size-3" />
                  Pinned
                </Badge>
              )}
            </div>
            <p className="font-mono text-sm text-muted-foreground">
              {scriptId}
            </p>
          </div>
        </div>
        {currentScript && (
          <TooltipProvider delayDuration={300}>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  variant="outline"
                  size="sm"
                  className={
                    isPinned
                      ? "gap-2 border-amber-500/30 text-amber-500 hover:text-amber-400"
                      : "gap-2"
                  }
                  disabled={pinMutation.isPending}
                  onClick={() =>
                    pinMutation.mutate({
                      cacheKeyValue: currentScript.cache_key_value,
                      pin: !isPinned,
                    })
                  }
                >
                  {isPinned ? (
                    <DrawingPinFilledIcon className="size-4" />
                  ) : (
                    <DrawingPinIcon className="size-4" />
                  )}
                  {isPinned ? "Unpin" : "Pin"}
                </Button>
              </TooltipTrigger>
              <TooltipContent side="bottom">
                {isPinned
                  ? "Unpin script to allow auto-updates"
                  : "Pin script to prevent auto-updates"}
              </TooltipContent>
            </Tooltip>
          </TooltipProvider>
        )}
      </header>

      <div className="grid grid-cols-4 gap-4">
        <div className="rounded-md border p-4">
          <p className="text-sm text-muted-foreground">Viewing Revision</p>
          <div className="flex flex-col gap-1">
            <div className="flex items-center gap-2">
              <p className="text-2xl font-semibold">
                {versionsLoading ? (
                  <Skeleton className="h-8 w-12" />
                ) : (
                  `#${activeVersion ?? "?"}`
                )}
              </p>
              {!versionsLoading && activeVersion != null && (
                <Badge
                  variant="secondary"
                  className={
                    isLatest
                      ? "text-xs"
                      : "border-amber-500/30 bg-amber-500/10 text-xs text-amber-500"
                  }
                >
                  {isLatest ? "Latest" : `${newerCount} newer`}
                </Badge>
              )}
            </div>
            {!versionsLoading && activeVersionInfo && (
              <p className="font-mono text-xs text-muted-foreground">
                {activeVersionInfo.script_revision_id}
              </p>
            )}
          </div>
        </div>
        <div className="rounded-md border p-4">
          <p className="text-sm text-muted-foreground">Revision History</p>
          <p className="text-2xl font-semibold">
            {versionsLoading ? (
              <Skeleton className="h-8 w-12" />
            ) : (
              <>
                {versions?.versions
                  ? versions.versions.filter(
                      (v) => v.version < (activeVersion ?? 0),
                    ).length
                  : 0}{" "}
                <span className="text-sm font-normal text-muted-foreground">
                  prior
                </span>
              </>
            )}
          </p>
        </div>
        <div className="rounded-md border p-4">
          <p className="text-sm text-muted-foreground">Runs (this revision)</p>
          <p className="text-2xl font-semibold">
            {runsLoading ? <Skeleton className="h-8 w-12" /> : totalCount}
          </p>
        </div>
        <div className="rounded-md border p-4">
          <p className="text-sm text-muted-foreground">Success Rate</p>
          <p className="text-2xl font-semibold">
            {runsLoading ? (
              <Skeleton className="h-8 w-12" />
            ) : successRate != null ? (
              <span
                className={
                  successRate >= 0.8
                    ? "text-green-500"
                    : successRate >= 0.5
                      ? "text-yellow-500"
                      : "text-red-500"
                }
              >
                {Math.round(successRate * 100)}%
              </span>
            ) : (
              "N/A"
            )}
          </p>
        </div>
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">Run Status Distribution</h2>
        {runsLoading ? (
          <Skeleton className="h-10 w-full" />
        ) : (
          <StatusDistribution
            statusCounts={statusCounts}
            totalCount={totalCount}
          />
        )}
      </div>

      <div className="space-y-3">
        <div className="flex items-center justify-between gap-4">
          <h2 className="whitespace-nowrap text-lg font-semibold">
            Script Code
          </h2>
          {workflowPermanentId && mainScript && (
            <ScriptFixInput
              workflowPermanentId={workflowPermanentId}
              workflowRunId={activeVersionInfo?.run_id ?? undefined}
            />
          )}
        </div>
        {!codeLoading && !versionsLoading && activeVersionInfo?.run_id && (
          <p className="text-sm text-muted-foreground">
            Revision #{activeVersion} created on run{" "}
            <Link
              to={`/workflows/${workflowPermanentId}/${activeVersionInfo.run_id}/code`}
              className="font-mono text-blue-400 hover:underline"
            >
              {activeVersionInfo.run_id}
            </Link>
          </p>
        )}
        {codeLoading || versionsLoading ? (
          <Skeleton className="h-64 w-full" />
        ) : mainScript ? (
          <div className="max-h-[500px] overflow-auto rounded-md border">
            <CodeEditor value={mainScript} readOnly language="python" />
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No code available for this script.
          </p>
        )}
      </div>

      <div className="space-y-3">
        <h2 className="text-lg font-semibold">
          Recent Runs{" "}
          <span className="text-sm font-normal text-muted-foreground">
            {totalCount > MAX_RUNS_SHOWN
              ? `(showing ${runs.length} of ${totalCount})`
              : `(${totalCount})`}
          </span>
        </h2>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Run ID</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Finished</TableHead>
                <TableHead>Failure Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runsLoading ? (
                <TableRow>
                  <TableCell colSpan={5}>
                    <Skeleton className="h-6 w-full" />
                  </TableCell>
                </TableRow>
              ) : runs.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-8 text-center text-muted-foreground"
                  >
                    No runs found for this script.
                  </TableCell>
                </TableRow>
              ) : (
                runs.map((run) => (
                  <TableRow key={run.workflow_run_id}>
                    <TableCell>
                      <Link
                        to={`/workflows/${workflowPermanentId}/${run.workflow_run_id}/overview`}
                        className="font-mono text-sm text-blue-400 hover:underline"
                      >
                        {run.workflow_run_id}
                      </Link>
                    </TableCell>
                    <TableCell>
                      <Badge variant={statusVariant[run.status] ?? "secondary"}>
                        {run.status}
                      </Badge>
                    </TableCell>
                    <TableCell
                      title={
                        run.started_at ? basicTimeFormat(run.started_at) : ""
                      }
                    >
                      {run.started_at
                        ? basicLocalTimeFormat(run.started_at)
                        : "-"}
                    </TableCell>
                    <TableCell
                      title={
                        run.finished_at ? basicTimeFormat(run.finished_at) : ""
                      }
                    >
                      {run.finished_at
                        ? basicLocalTimeFormat(run.finished_at)
                        : "-"}
                    </TableCell>
                    <TableCell
                      className="max-w-xs truncate"
                      title={run.failure_reason ?? ""}
                    >
                      {run.failure_reason ?? "-"}
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}

export { WorkflowScriptDetailPage };
