import { Status } from "@/api/types";
import {
  hasExtractedInformation,
  isAction,
  isActionItem,
  isObserverThought,
  isWorkflowRunBlock,
} from "../types/workflowRunTypes";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { CodeEditor } from "../components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { WorkflowBlockTypes } from "../types/workflowTypes";
import { statusIsAFailureType } from "@/routes/tasks/types";
import { WorkflowRunOverviewActiveElement } from "./WorkflowRunOverview";
import { ExternalLinkIcon } from "@radix-ui/react-icons";
import { Link } from "react-router-dom";
import { SendEmailBlockParameters } from "./blockInfo/SendEmailBlockInfo";

type Props = {
  activeItem: WorkflowRunOverviewActiveElement;
};

function WorkflowRunTimelineItemInfoSection({ activeItem }: Props) {
  const item = isActionItem(activeItem) ? activeItem.block : activeItem;

  if (!item) {
    return null;
  }
  if (item === "stream") {
    return null;
  }
  if (isAction(item)) {
    return null;
  }
  if (isObserverThought(item)) {
    return (
      <div className="rounded bg-slate-elevation1 p-4">
        <Tabs key="thought" defaultValue="observation">
          <TabsList>
            <TabsTrigger value="observation">Observation</TabsTrigger>
            <TabsTrigger value="thought">Thought</TabsTrigger>
            <TabsTrigger value="answer">Answer</TabsTrigger>
          </TabsList>
          <TabsContent value="observation">
            <AutoResizingTextarea value={item.observation ?? ""} readOnly />
          </TabsContent>
          <TabsContent value="thought">
            <AutoResizingTextarea value={item.thought ?? ""} readOnly />
          </TabsContent>
          <TabsContent value="answer">
            <AutoResizingTextarea value={item.answer ?? ""} readOnly />
          </TabsContent>
        </Tabs>
      </div>
    );
  }
  if (isWorkflowRunBlock(item)) {
    const showExtractedInformationTab = item.status === Status.Completed;
    const showFailureReasonTab =
      item.status && statusIsAFailureType({ status: item.status });
    const defaultTab = showExtractedInformationTab
      ? "extracted_information"
      : showFailureReasonTab
        ? "failure_reason"
        : "navigation_goal";
    if (
      item.block_type === WorkflowBlockTypes.Task ||
      item.block_type === WorkflowBlockTypes.Navigation ||
      item.block_type === WorkflowBlockTypes.Action ||
      item.block_type === WorkflowBlockTypes.Extraction ||
      item.block_type === WorkflowBlockTypes.Validation ||
      item.block_type === WorkflowBlockTypes.Login ||
      item.block_type === WorkflowBlockTypes.FileDownload
    ) {
      return (
        <div className="rounded bg-slate-elevation1 p-4">
          <Tabs key={item.task_id ?? item.block_type} defaultValue={defaultTab}>
            <TabsList>
              {item.status === Status.Completed && (
                <TabsTrigger value="extracted_information">
                  Extracted Information
                </TabsTrigger>
              )}
              {item.status && statusIsAFailureType({ status: item.status }) && (
                <TabsTrigger value="failure_reason">Failure Reason</TabsTrigger>
              )}
              <TabsTrigger value="navigation_goal">Navigation Goal</TabsTrigger>
              <TabsTrigger value="parameters">Parameters</TabsTrigger>
              {item.task_id && (
                <Link
                  to={`/tasks/${item.task_id}/diagnostics`}
                  title="Go to diagnostics"
                  onClick={(event) => event.stopPropagation()}
                >
                  <div className="flex items-center gap-2 px-3 py-1 text-sm font-medium">
                    <ExternalLinkIcon />
                    <span>Diagnostics</span>
                  </div>
                </Link>
              )}
            </TabsList>
            {item.status === Status.Completed && (
              <TabsContent value="extracted_information">
                <CodeEditor
                  language="json"
                  value={JSON.stringify(
                    (hasExtractedInformation(item.output) &&
                      item.output.extracted_information) ??
                      null,
                    null,
                    2,
                  )}
                  minHeight="96px"
                  maxHeight="500px"
                  readOnly
                />
              </TabsContent>
            )}
            {item.status && statusIsAFailureType({ status: item.status }) && (
              <TabsContent value="failure_reason">
                <AutoResizingTextarea
                  value={
                    item.status === "canceled"
                      ? "This block was cancelled"
                      : item.failure_reason ?? ""
                  }
                  readOnly
                />
              </TabsContent>
            )}
            <TabsContent value="navigation_goal">
              <AutoResizingTextarea
                value={item.navigation_goal ?? ""}
                readOnly
              />
            </TabsContent>
            <TabsContent value="parameters">
              <CodeEditor
                value={JSON.stringify(item.navigation_payload, null, 2)}
                minHeight="96px"
                maxHeight="500px"
                language="json"
                readOnly
              />
            </TabsContent>
          </Tabs>
        </div>
      );
    }
    if (item.block_type === WorkflowBlockTypes.SendEmail) {
      if (
        item.body !== null &&
        typeof item.body !== "undefined" &&
        item.recipients !== null &&
        typeof item.recipients !== "undefined" &&
        item.subject !== null &&
        typeof item.subject !== "undefined"
      ) {
        return (
          <SendEmailBlockParameters
            body={item.body}
            recipients={item.recipients}
            subject={item.subject}
          />
        );
      }
      return null;
    }

    if (item.block_type === WorkflowBlockTypes.TextPrompt) {
      if (item.prompt !== null) {
        return (
          <div className="rounded bg-slate-elevation1 p-4">
            <Tabs key={item.block_type} defaultValue="prompt">
              <TabsList>
                <TabsTrigger value="prompt">Prompt</TabsTrigger>
                <TabsTrigger value="output">Output</TabsTrigger>
              </TabsList>
              <TabsContent value="prompt">
                <CodeEditor
                  value={item.prompt ?? ""}
                  minHeight="96px"
                  maxHeight="500px"
                  readOnly
                />
              </TabsContent>
              <TabsContent value="output">
                <CodeEditor
                  value={JSON.stringify(item.output, null, 2)}
                  minHeight="96px"
                  maxHeight="500px"
                  language="json"
                  readOnly
                />
              </TabsContent>
            </Tabs>
          </div>
        );
      }
      return null;
    }

    if (item.block_type === WorkflowBlockTypes.Wait) {
      if (item.wait_sec !== null && typeof item.wait_sec !== "undefined") {
        return (
          <div className="flex w-1/2 justify-between rounded bg-slate-elevation1 p-4">
            <span className="text-sm text-slate-400">Wait Time</span>
            <span className="text-sm">{item.wait_sec} Seconds</span>
          </div>
        );
      }
      return null;
    }

    return (
      <div className="rounded bg-slate-elevation1 p-4">
        <Tabs key={item.block_type} defaultValue="output">
          <TabsList>
            <TabsTrigger value="output">Output</TabsTrigger>
          </TabsList>
          <TabsContent value="output">
            <CodeEditor
              value={JSON.stringify(item.output, null, 2)}
              minHeight="96px"
              maxHeight="500px"
              language="json"
              readOnly
            />
          </TabsContent>
        </Tabs>
      </div>
    );
  }
}

export { WorkflowRunTimelineItemInfoSection };
