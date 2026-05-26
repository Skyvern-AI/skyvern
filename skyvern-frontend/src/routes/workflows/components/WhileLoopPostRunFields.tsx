import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { HelpTooltip } from "@/components/HelpTooltip";
import { CodeEditor } from "./CodeEditor";
import { isBlockOfType } from "../workflowBlockUtils";
import { WorkflowBlockTypes, type WorkflowBlock } from "../types/workflowTypes";

const WHILE_CONDITION_HELP =
  "Evaluated each iteration until false or the loop exits.";
const BLOCK_OUTPUT_HELP =
  "Structured output from this loop container after the run.";

type WhileLoopPostRunFieldsLayout = "sidebar" | "stacked";

type WhileLoopPostRunFieldsProps = {
  layout: WhileLoopPostRunFieldsLayout;
  definitionBlock: WorkflowBlock | null;
  loopOutput: unknown;
};

export function WhileLoopPostRunFields({
  layout,
  definitionBlock,
  loopOutput,
}: WhileLoopPostRunFieldsProps) {
  const whileDefinition = isBlockOfType(
    definitionBlock,
    WorkflowBlockTypes.WhileLoop,
  )
    ? definitionBlock
    : null;

  const outputJson = JSON.stringify(loopOutput ?? null, null, 2);

  if (layout === "sidebar") {
    return (
      <>
        {whileDefinition ? (
          <div className="flex gap-16">
            <div className="w-80">
              <h1 className="text-sm">While condition</h1>
              <h2 className="text-sm text-slate-400">{WHILE_CONDITION_HELP}</h2>
            </div>
            <div className="w-full space-y-2">
              <div className="text-xs capitalize text-slate-500">
                {whileDefinition.condition.criteria_type.replace(/_/g, " ")}
              </div>
              <AutoResizingTextarea
                value={whileDefinition.condition.expression}
                readOnly
                className="font-mono text-sm"
              />
            </div>
          </div>
        ) : null}
        <div className="flex gap-16">
          <div className="w-80">
            <h1 className="text-sm">Block output</h1>
            <h2 className="text-sm text-slate-400">{BLOCK_OUTPUT_HELP}</h2>
          </div>
          <CodeEditor
            className="w-full"
            language="json"
            value={outputJson}
            readOnly
            minHeight="96px"
            maxHeight="200px"
          />
        </div>
      </>
    );
  }

  return (
    <>
      {whileDefinition ? (
        <div className="flex flex-col gap-2">
          <div className="flex w-full items-center justify-start gap-2">
            <h1 className="text-sm">While condition</h1>
            <HelpTooltip content={WHILE_CONDITION_HELP} />
          </div>
          <div className="text-xs capitalize text-slate-500">
            {whileDefinition.condition.criteria_type.replace(/_/g, " ")}
          </div>
          <AutoResizingTextarea
            value={whileDefinition.condition.expression}
            readOnly
            className="font-mono text-sm"
          />
        </div>
      ) : null}
      <div className="flex flex-col gap-2">
        <div className="flex w-full items-center justify-start gap-2">
          <h1 className="text-sm">Block output</h1>
          <HelpTooltip content={BLOCK_OUTPUT_HELP} />
        </div>
        <CodeEditor
          className="w-full"
          language="json"
          value={outputJson}
          readOnly
          minHeight="96px"
          maxHeight="200px"
        />
      </div>
    </>
  );
}
