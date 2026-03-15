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
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { FileTextIcon, Pencil2Icon, PlayIcon } from "@radix-ui/react-icons";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useWorkflowQuery } from "./hooks/useWorkflowQuery";
import { useWorkflowScriptsQuery } from "./hooks/useWorkflowScriptsQuery";
import { WorkflowActions } from "./WorkflowActions";
import type { WorkflowScriptSummary } from "./types/scriptTypes";

function ScriptsTableRows({
  isLoading,
  isError,
  scripts,
}: {
  isLoading: boolean;
  isError: boolean;
  scripts: WorkflowScriptSummary[];
}) {
  if (isLoading) {
    return (
      <TableRow>
        <TableCell colSpan={5}>
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
        <TableCell colSpan={5}>
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
        <TableCell colSpan={5}>
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
    // TODO: link to script detail view in Phase 2
    <TableRow key={script.script_id}>
      <TableCell className="font-mono text-sm">
        {script.cache_key_value || "(default)"}
      </TableCell>
      <TableCell>{script.version_count}</TableCell>
      <TableCell>v{script.latest_version}</TableCell>
      <TableCell>
        <Badge
          variant={script.status === "published" ? "default" : "secondary"}
        >
          {script.status}
        </Badge>
      </TableCell>
      <TableCell title={basicTimeFormat(script.modified_at)}>
        {basicLocalTimeFormat(script.modified_at)}
      </TableCell>
    </TableRow>
  ));
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
              <h1 className="text-lg font-semibold">{workflow?.title}</h1>
              <h2 className="text-sm">{workflowPermanentId}</h2>
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
          <Button asChild variant="outline" size="sm">
            <Link to={`/workflows/${workflowPermanentId}/runs`}>View Runs</Link>
          </Button>
        </header>
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-1/4">Cache Key Value</TableHead>
                <TableHead className="w-1/6">Versions</TableHead>
                <TableHead className="w-1/6">Latest</TableHead>
                <TableHead className="w-1/6">Status</TableHead>
                <TableHead className="w-1/4">Last Updated</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <ScriptsTableRows
                isLoading={scriptsLoading}
                isError={isError}
                scripts={scripts}
              />
            </TableBody>
          </Table>
        </div>
      </div>
    </div>
  );
}

export { WorkflowScriptsPage };
