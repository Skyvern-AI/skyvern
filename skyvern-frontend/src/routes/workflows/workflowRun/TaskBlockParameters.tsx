import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { WorkflowRunBlock } from "../types/workflowRunTypes";
import { isTaskVariantBlock, WorkflowBlockTypes } from "../types/workflowTypes";
import { Switch } from "@/components/ui/switch";

type Props = {
  block: WorkflowRunBlock;
};

function TaskBlockParameters({ block }: Props) {
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
      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">URL</h1>
          <h2 className="text-base text-slate-400">
            The starting URL for the block
          </h2>
        </div>
        <Input value={block.url ?? ""} readOnly />
      </div>

      {showNavigationParameters ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Navigation Goal</h1>
            <h2 className="text-base text-slate-400">
              What should Skyvern do on this page?
            </h2>
          </div>
          <AutoResizingTextarea value={block.navigation_goal ?? ""} readOnly />
        </div>
      ) : null}

      {showNavigationParameters ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Navigation Payload</h1>
            <h2 className="text-base text-slate-400">
              Specify important parameters, routes, or states
            </h2>
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
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Data Extraction Goal</h1>
            <h2 className="text-base text-slate-400">
              What outputs are you looking to get?
            </h2>
          </div>
          <AutoResizingTextarea
            value={block.data_extraction_goal ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showDataExtractionParameters ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Data Schema</h1>
            <h2 className="text-base text-slate-400">
              Specify the output format in JSON
            </h2>
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
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Completion Criteria</h1>
            <h2 className="text-base text-slate-400">Complete if...</h2>
          </div>
          <AutoResizingTextarea
            value={block.complete_criterion ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showValidationParameters ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Termination Criteria</h1>
            <h2 className="text-base text-slate-400">Terminate if...</h2>
          </div>
          <AutoResizingTextarea
            value={block.terminate_criterion ?? ""}
            readOnly
          />
        </div>
      ) : null}

      {showIncludeActionHistoryInVerification ? (
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-lg">Include Action History</h1>
            <h2 className="text-base text-slate-400">
              Whether to include action history in the completion verification
            </h2>
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

export { TaskBlockParameters };
