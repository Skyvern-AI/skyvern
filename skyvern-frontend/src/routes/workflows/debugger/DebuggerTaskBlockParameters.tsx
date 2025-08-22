import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { WorkflowRunBlock } from "../types/workflowRunTypes";
import { isTaskVariantBlock, WorkflowBlockTypes } from "../types/workflowTypes";
import { Switch } from "@/components/ui/switch";
import { HelpTooltip } from "@/components/HelpTooltip";

type Props = {
  block: WorkflowRunBlock;
};

function DebuggerTaskBlockParameters({ block }: Props) {
  const isTaskVariant = isTaskVariantBlock(block);
  if (!isTaskVariant) {
    return null;
  }

  const showNavigationParameters =
    block.block_type === WorkflowBlockTypes.Task ||
    block.block_type === WorkflowBlockTypes.Action ||
    block.block_type === WorkflowBlockTypes.Login ||
    block.block_type === WorkflowBlockTypes.Navigation;

  const showDataExtractionParameters =
    block.block_type === WorkflowBlockTypes.Task ||
    block.block_type === WorkflowBlockTypes.Extraction;

  const showValidationParameters =
    block.block_type === WorkflowBlockTypes.Validation;

  const showIncludeActionHistoryInVerification =
    block.block_type === WorkflowBlockTypes.Task ||
    block.block_type === WorkflowBlockTypes.Navigation;

  return (
    <>
      <div className="flex flex-col gap-2">
        <div className="flex w-full items-center justify-start gap-2">
          <h1 className="text-sm">URL</h1>
          <HelpTooltip content="The starting URL for the block." />
        </div>
        <Input value={block.url ?? ""} readOnly />
      </div>

      {showNavigationParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">Navigation Goal</h1>
            <HelpTooltip content="What should Skyvern do on this page?" />
          </div>
          <AutoResizingTextarea value={block.navigation_goal ?? ""} readOnly />
        </div>
      ) : null}

      {showNavigationParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-nowrap text-sm">Navigation Payload</h1>
            <HelpTooltip content="Specify important parameters, routes, or states." />
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={JSON.stringify(block.navigation_payload, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      ) : null}

      {showDataExtractionParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">Data Extraction Goal</h1>
            <HelpTooltip content="What outputs are you looking to get?" />
          </div>
          <AutoResizingTextarea
            value={block.data_extraction_goal ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showDataExtractionParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">Data Schema</h1>
            <HelpTooltip content="Specify the output format in JSON" />
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={JSON.stringify(block.data_schema, null, 2)}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      ) : null}

      {showValidationParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">Completion Criteria</h1>
            <HelpTooltip content="Complete if..." />
          </div>
          <AutoResizingTextarea
            value={block.complete_criterion ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showValidationParameters ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">Termination Criteria</h1>
            <HelpTooltip content="Terminate if..." />
          </div>
          <AutoResizingTextarea
            value={block.terminate_criterion ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showIncludeActionHistoryInVerification ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-nowrap text-sm">Include Action History</h1>
            <HelpTooltip content="Whether to include action history in the completion verification" />
          </div>
          <div className="w-full">
            <Switch
              checked={block.include_action_history_in_verification ?? false}
              disabled
            />
          </div>
        </div>
      ) : null}
    </>
  );
}

export { DebuggerTaskBlockParameters };
