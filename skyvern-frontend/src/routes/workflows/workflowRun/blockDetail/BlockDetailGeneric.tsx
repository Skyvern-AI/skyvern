import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { BlockDetailFailure, GoalText, Section } from "./shared";

type Props = {
  block: WorkflowRunBlock;
};

function BlockDetailGeneric({ block }: Props) {
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      {block.prompt && (
        <Section title="Prompt">
          <GoalText text={block.prompt} />
        </Section>
      )}
      {block.body && (
        <Section title="Body">
          <GoalText text={block.body} />
        </Section>
      )}
    </div>
  );
}

export { BlockDetailGeneric };
