import type { ObserverThought } from "../../types/workflowRunTypes";
import { GoalText, Section } from "./shared";

type Props = {
  thought: ObserverThought;
};

function BlockDetailThought({ thought }: Props) {
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      {thought.user_input && (
        <Section title="User input">
          <GoalText text={thought.user_input} />
        </Section>
      )}
      {thought.observation && (
        <Section title="Observation">
          <GoalText text={thought.observation} />
        </Section>
      )}
      {thought.thought && (
        <Section title="Thought">
          <GoalText text={thought.thought} />
        </Section>
      )}
      {thought.answer && (
        <Section title="Answer">
          <GoalText text={thought.answer} />
        </Section>
      )}
    </div>
  );
}

export { BlockDetailThought };
