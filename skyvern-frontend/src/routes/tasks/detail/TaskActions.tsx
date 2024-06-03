import { useState } from "react";
import { useParams } from "react-router-dom";
import { ActionScreenshot } from "./ActionScreenshot";
import { InputReasoningCard } from "./InputReasoningCard";
import { ScrollableActionList } from "./ScrollableActionList";
import { useActions } from "./useActions";

function TaskActions() {
  const { taskId } = useParams();

  const { data, isFetching } = useActions(taskId!);
  const [selectedActionIndex, setSelectedAction] = useState(0);

  const activeAction = data?.[selectedActionIndex];

  if (isFetching || !data) {
    return <div>Loading...</div>;
  }

  if (!activeAction) {
    return <div>No action</div>;
  }

  return (
    <div className="flex gap-2">
      <div className="w-3/4 h-[40rem] border rounded">
        <div className="p-4">
          <InputReasoningCard
            input={activeAction.input}
            reasoning={activeAction.reasoning}
            confidence={activeAction.confidence}
          />
        </div>
        <div className="p-4">
          <div className="p-4">
            <ActionScreenshot
              stepId={activeAction.stepId}
              index={activeAction.index}
            />
          </div>
        </div>
      </div>
      <ScrollableActionList
        activeIndex={selectedActionIndex}
        data={data}
        onActiveIndexChange={setSelectedAction}
        onNext={() =>
          setSelectedAction((prev) =>
            prev === data.length - 1 ? prev : prev + 1,
          )
        }
        onPrevious={() =>
          setSelectedAction((prev) => (prev === 0 ? prev : prev - 1))
        }
      />
    </div>
  );
}

export { TaskActions };
