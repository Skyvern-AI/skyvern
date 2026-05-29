import type { ActionsApiResponse } from "@/api/types";
import type {
  ObserverThought,
  WorkflowRunBlock,
} from "../../types/workflowRunTypes";
import type { WorkflowRunOverviewActiveElement } from "../WorkflowRunOverview";
import {
  BlockActionList,
  BlockDetailFailure,
  BlockThoughtList,
} from "./shared";

type Props = {
  block: WorkflowRunBlock;
  activeItem: WorkflowRunOverviewActiveElement;
  thoughts?: Array<ObserverThought>;
  onActionSelect?: (payload: {
    block: WorkflowRunBlock;
    action: ActionsApiResponse;
  }) => void;
  onThoughtSelect?: (thought: ObserverThought) => void;
};

function BlockDetailTask({
  block,
  activeItem,
  thoughts = [],
  onActionSelect,
  onThoughtSelect,
}: Props) {
  return (
    <div className="space-y-4 px-3 py-3 empty:hidden">
      <BlockDetailFailure block={block} />
      <BlockActionList
        block={block}
        activeItem={activeItem}
        onActionSelect={onActionSelect}
      />
      <BlockThoughtList
        thoughts={thoughts}
        activeItem={activeItem}
        onSelect={onThoughtSelect}
      />
    </div>
  );
}

export { BlockDetailTask };
