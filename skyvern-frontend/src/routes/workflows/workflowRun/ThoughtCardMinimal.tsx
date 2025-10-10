import { ObserverThought } from "../types/workflowRunTypes";
import { Tip } from "@/components/Tip";
import { BrainIcon } from "@/components/icons/BrainIcon";

type Props = {
  thought: ObserverThought;
};

function ThoughtCardMinimal({ thought }: Props) {
  return (
    <Tip asChild={false} content={thought.answer || thought.thought || null}>
      <BrainIcon className="size-6" />
    </Tip>
  );
}

export { ThoughtCardMinimal };
