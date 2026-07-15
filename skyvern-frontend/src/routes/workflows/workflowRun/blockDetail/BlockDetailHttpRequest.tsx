import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { JsonExplorer } from "./BlockInspector";
import { BlockDetailFailure, Section } from "./shared";

type Props = {
  block: WorkflowRunBlock;
};

function BlockDetailHttpRequest({ block }: Props) {
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      {block.url && (
        <Section title="URL">
          <span className="break-all text-xs text-tertiary-foreground">
            {block.url}
          </span>
        </Section>
      )}
      {block.output !== null && block.output !== undefined && (
        <Section title="Response">
          <JsonExplorer value={block.output} rootLabel="response" />
        </Section>
      )}
    </div>
  );
}

export { BlockDetailHttpRequest };
