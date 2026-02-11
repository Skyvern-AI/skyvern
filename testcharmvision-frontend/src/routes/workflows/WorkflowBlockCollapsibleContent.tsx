import { Status, TaskApiResponse } from "@/api/types";
import { StatusBadge } from "@/components/StatusBadge";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { TableCell, TableRow } from "@/components/ui/table";
import { basicLocalTimeFormat, basicTimeFormat } from "@/util/timeFormat";
import { ChevronDownIcon, ChevronRightIcon } from "@radix-ui/react-icons";
import { useState } from "react";
import { cn } from "@/util/utils";
import { CodeEditor } from "./components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { statusIsFinalized } from "../tasks/types";

type Props = {
  task: TaskApiResponse;
  onNavigate: (event: React.MouseEvent, id: string) => void;
};

function WorkflowBlockCollapsibleContent({ task, onNavigate }: Props) {
  const [open, setOpen] = useState(false);
  const [activeTab, setActiveTab] = useState<"output" | "goal" | "parameters">(
    statusIsFinalized(task) ? "output" : "goal",
  );

  const showExtractedInformation = task?.status === Status.Completed;
  const extractedInformation = showExtractedInformation ? (
    <CodeEditor
      language="json"
      value={JSON.stringify(task.extracted_information, null, 2)}
      readOnly
      minHeight={"96px"}
      maxHeight={"500px"}
      className="w-full"
    />
  ) : null;

  const isCanceled = task?.status === Status.Canceled;

  const showFailureReason =
    task?.status === Status.Failed ||
    task?.status === Status.Terminated ||
    task?.status === Status.TimedOut ||
    task?.status === Status.Canceled;

  const failureReason = showFailureReason ? (
    <CodeEditor
      language="json"
      value={JSON.stringify(
        task.failure_reason ?? (isCanceled && "This task was canceled."),
        null,
        2,
      )}
      readOnly
      minHeight={"96px"}
      maxHeight={"500px"}
      className="w-full"
    />
  ) : null;

  return (
    <Collapsible key={task.task_id} asChild open={open} onOpenChange={setOpen}>
      <>
        <TableRow
          className={cn("hover:bg-slate-elevation2", {
            "border-b-0 bg-slate-elevation2": open,
          })}
        >
          <TableCell>
            <CollapsibleTrigger asChild>
              <div className="w-10 cursor-pointer rounded-full p-2 hover:bg-muted">
                {open ? (
                  <ChevronDownIcon className="size-6" />
                ) : (
                  <ChevronRightIcon className="size-6" />
                )}
              </div>
            </CollapsibleTrigger>
          </TableCell>
          <TableCell
            className="w-1/5 max-w-0 cursor-pointer truncate"
            title={task.request.title ?? undefined}
            onClick={(event) => {
              onNavigate(event, task.task_id);
            }}
          >
            {task.request.title}
          </TableCell>
          <TableCell
            className="w-1/6 max-w-0 cursor-pointer truncate"
            title={task.task_id}
            onClick={(event) => {
              onNavigate(event, task.task_id);
            }}
          >
            {task.task_id}
          </TableCell>
          <TableCell
            className="w-1/4 max-w-0 cursor-pointer truncate"
            title={task.request.url}
            onClick={(event) => onNavigate(event, task.task_id)}
          >
            {task.request.url}
          </TableCell>
          <TableCell
            className="w-1/8 cursor-pointer"
            onClick={(event) => onNavigate(event, task.task_id)}
          >
            <StatusBadge status={task.status} />
          </TableCell>
          <TableCell
            className="w-1/5 max-w-0 cursor-pointer truncate"
            onClick={(event) => onNavigate(event, task.task_id)}
            title={basicTimeFormat(task.created_at)}
          >
            {basicLocalTimeFormat(task.created_at)}
          </TableCell>
        </TableRow>
        <CollapsibleContent asChild>
          <TableRow className="bg-slate-elevation2 hover:bg-slate-elevation2">
            <TableCell colSpan={6} className="border-b">
              <div className="space-y-2 px-6">
                <div className="flex gap-1">
                  {statusIsFinalized(task) && (
                    <div
                      className={cn(
                        "cursor-pointer rounded-sm px-3 py-2 text-slate-400 hover:bg-slate-700",
                        {
                          "bg-slate-700 text-foreground":
                            activeTab === "output",
                        },
                      )}
                      onClick={() => {
                        setActiveTab("output");
                      }}
                    >
                      {showExtractedInformation
                        ? "Extracted Information"
                        : showFailureReason
                          ? "Failure Reason"
                          : ""}
                    </div>
                  )}

                  <div
                    className={cn(
                      "cursor-pointer rounded-sm px-3 py-2 text-slate-400 hover:bg-slate-700 hover:text-foreground",
                      {
                        "bg-slate-700 text-foreground": activeTab === "goal",
                      },
                    )}
                    onClick={() => {
                      setActiveTab("goal");
                    }}
                  >
                    Navigation Goal
                  </div>
                  <div
                    className={cn(
                      "cursor-pointer rounded-sm px-3 py-2 text-slate-400 hover:bg-slate-700 hover:text-foreground",
                      {
                        "bg-slate-700 text-foreground":
                          activeTab === "parameters",
                      },
                    )}
                    onClick={() => {
                      setActiveTab("parameters");
                    }}
                  >
                    Parameters
                  </div>
                </div>
                <div>
                  {activeTab === "output" &&
                    (showExtractedInformation
                      ? extractedInformation
                      : showFailureReason
                        ? failureReason
                        : null)}
                  {activeTab === "goal" && (
                    <AutoResizingTextarea
                      value={task.request.navigation_goal ?? ""}
                      readOnly
                    />
                  )}
                  {activeTab === "parameters" && (
                    <CodeEditor
                      language="json"
                      value={JSON.stringify(
                        task.request.navigation_payload,
                        null,
                        2,
                      )}
                      minHeight={"96px"}
                      maxHeight={"500px"}
                      className="w-full"
                      readOnly
                    />
                  )}
                </div>
              </div>
            </TableCell>
          </TableRow>
        </CollapsibleContent>
      </>
    </Collapsible>
  );
}

export { WorkflowBlockCollapsibleContent };
