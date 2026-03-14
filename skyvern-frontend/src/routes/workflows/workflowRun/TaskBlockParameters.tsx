import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";
import { WorkflowRunBlock } from "../types/workflowRunTypes";
import {
  isTaskVariantBlock,
  WorkflowBlockTypes,
  type WorkflowBlock,
} from "../types/workflowTypes";
import { Switch } from "@/components/ui/switch";

type Props = {
  block: WorkflowRunBlock;
  definitionBlock?: WorkflowBlock | null;
};

function TaskBlockParameters({ block, definitionBlock }: Props) {
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

  // Advanced settings from definition block
  const errorCodeMapping =
    definitionBlock &&
    "error_code_mapping" in definitionBlock &&
    definitionBlock.error_code_mapping
      ? definitionBlock.error_code_mapping
      : null;

  const maxRetries =
    definitionBlock && "max_retries" in definitionBlock
      ? definitionBlock.max_retries
      : undefined;

  const maxStepsPerRun =
    definitionBlock && "max_steps_per_run" in definitionBlock
      ? definitionBlock.max_steps_per_run
      : undefined;

  const totpVerificationUrl =
    definitionBlock && "totp_verification_url" in definitionBlock
      ? definitionBlock.totp_verification_url
      : null;

  const totpIdentifier =
    definitionBlock && "totp_identifier" in definitionBlock
      ? definitionBlock.totp_identifier
      : null;

  const completeOnDownload =
    definitionBlock && "complete_on_download" in definitionBlock
      ? definitionBlock.complete_on_download
      : undefined;

  const downloadSuffix =
    definitionBlock && "download_suffix" in definitionBlock
      ? definitionBlock.download_suffix
      : null;

  const disableCache =
    definitionBlock && "disable_cache" in definitionBlock
      ? definitionBlock.disable_cache
      : undefined;

  const engine =
    block.engine ??
    (definitionBlock && "engine" in definitionBlock
      ? definitionBlock.engine
      : null);

  const hasAdvancedSettings =
    errorCodeMapping !== null ||
    typeof maxRetries === "number" ||
    typeof maxStepsPerRun === "number" ||
    Boolean(totpVerificationUrl) ||
    Boolean(totpIdentifier) ||
    completeOnDownload === true ||
    Boolean(downloadSuffix) ||
    disableCache === true ||
    engine !== null;

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

      <div className="flex gap-16">
        <div className="w-80">
          <h1 className="text-lg">Continue on Failure</h1>
          <h2 className="text-base text-slate-400">
            Whether to continue the workflow if this block fails
          </h2>
        </div>
        <div className="flex w-full items-center gap-3">
          <Switch checked={block.continue_on_failure} disabled />
          <span className="text-sm text-slate-400">
            {block.continue_on_failure ? "Enabled" : "Disabled"}
          </span>
        </div>
      </div>

      {hasAdvancedSettings ? (
        <>
          <h2 className="text-base font-semibold text-slate-300">
            Advanced Settings
          </h2>

          {engine ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Engine</h1>
                <h2 className="text-base text-slate-400">
                  The execution engine used for this block
                </h2>
              </div>
              <Input value={engine} readOnly />
            </div>
          ) : null}

          {errorCodeMapping ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Error Code Mapping</h1>
                <h2 className="text-base text-slate-400">
                  Custom error codes and their descriptions
                </h2>
              </div>
              <CodeEditor
                className="w-full"
                language="json"
                value={JSON.stringify(errorCodeMapping, null, 2)}
                readOnly
                minHeight="96px"
                maxHeight="200px"
              />
            </div>
          ) : null}

          {typeof maxRetries === "number" ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Max Retries</h1>
              </div>
              <Input value={maxRetries.toString()} readOnly />
            </div>
          ) : null}

          {typeof maxStepsPerRun === "number" ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Max Steps Per Run</h1>
              </div>
              <Input value={maxStepsPerRun.toString()} readOnly />
            </div>
          ) : null}

          {totpVerificationUrl ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">TOTP Verification URL</h1>
              </div>
              <Input value={totpVerificationUrl} readOnly />
            </div>
          ) : null}

          {totpIdentifier ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">TOTP Identifier</h1>
              </div>
              <Input value={totpIdentifier} readOnly />
            </div>
          ) : null}

          {completeOnDownload === true ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Complete on Download</h1>
              </div>
              <div className="flex w-full items-center gap-3">
                <Switch checked={true} disabled />
                <span className="text-sm text-slate-400">Enabled</span>
              </div>
            </div>
          ) : null}

          {downloadSuffix ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Download Suffix</h1>
              </div>
              <Input value={downloadSuffix} readOnly />
            </div>
          ) : null}

          {disableCache === true ? (
            <div className="flex gap-16">
              <div className="w-80">
                <h1 className="text-lg">Cache Disabled</h1>
              </div>
              <div className="flex w-full items-center gap-3">
                <Switch checked={true} disabled />
                <span className="text-sm text-slate-400">
                  Cache is disabled for this block
                </span>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </>
  );
}

export { TaskBlockParameters };
