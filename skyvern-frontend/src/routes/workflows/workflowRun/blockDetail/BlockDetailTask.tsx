import type {
  ObserverThought,
  WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import type { WorkflowRunOverviewActiveElement } from "../WorkflowRunOverview";
import { BlockDetailFailure, BlockThoughtList } from "./shared";

type Props = {
  block: WorkflowRunBlock;
  activeItem: WorkflowRunOverviewActiveElement;
  thoughts?: Array<ObserverThought>;
  onThoughtSelect?: (thought: ObserverThought) => void;
};

function BlockDetailTask({
  block,
  activeItem,
  thoughts = [],
  onThoughtSelect,
}: Props) {
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      <BlockThoughtList
        thoughts={thoughts}
        activeItem={activeItem}
        onSelect={onThoughtSelect}
      />
    </div>
  );
}

export { BlockDetailTask };
