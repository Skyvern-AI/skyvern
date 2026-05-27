import type { WorkflowRunBlock } from "../../types/workflowRunTypes";
import { WorkflowRunHumanInteraction } from "../WorkflowRunHumanInteraction";
import { BlockDetailFailure, GoalText, Section } from "./shared";

type Props = {
  block: WorkflowRunBlock;
};

function BlockDetailHumanInteraction({ block }: Props) {
  const recipients = block.recipients ?? [];
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      {block.instructions && (
        <Section title="Instructions">
          <GoalText text={block.instructions} />
        </Section>
      )}
      {block.subject && (
        <Section title="Email subject">
          <span className="break-words text-xs text-slate-300">
            {block.subject}
          </span>
        </Section>
      )}
      {recipients.length > 0 && (
        <Section title="Recipients">
          <ul className="space-y-1 text-xs text-slate-300">
            {recipients.map((address) => (
              <li key={address} className="break-all">
                {address}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <WorkflowRunHumanInteraction workflowRunBlock={block} />
    </div>
  );
}

export { BlockDetailHumanInteraction };
