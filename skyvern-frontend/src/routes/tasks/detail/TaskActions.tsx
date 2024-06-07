import { useState } from "react";
import { useParams } from "react-router-dom";
import { ActionScreenshot } from "./ActionScreenshot";
import { ScrollableActionList } from "./ScrollableActionList";
import { useActions } from "./useActions";
import { Skeleton } from "@/components/ui/skeleton";

function TaskActions() {
  const { taskId } = useParams();

  const { data, isFetching } = useActions(taskId!);
  const [selectedActionIndex, setSelectedAction] = useState(0);

  const activeAction = data?.[selectedActionIndex];

  if (isFetching) {
    return (
      <div className="flex gap-2">
        <div className="h-[40rem] w-3/4">
          <Skeleton className="h-full" />
        </div>
        <div className="h-[40rem] w-1/4">
          <Skeleton className="h-full" />
        </div>
      </div>
    );
  }

  if (!data) {
    return <div>No actions</div>;
  }

  if (!activeAction) {
    return <div>No active action</div>;
  }

  return (
    <div className="flex gap-2">
      <div className="w-2/3 border rounded">
        <div className="p-4">
          <ActionScreenshot
            stepId={activeAction.stepId}
            index={activeAction.index}
          />
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
