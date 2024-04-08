import { useState } from "react";
import { StepNavigation } from "./StepNavigation";
import { StepArtifacts } from "./StepArtifacts";
import { useQuery } from "@tanstack/react-query";
import { StepApiResponse } from "@/api/types";
import { useParams } from "react-router-dom";
import { client } from "@/api/AxiosClient";

function StepArtifactsLayout() {
  const [activeIndex, setActiveIndex] = useState(0);
  const { taskId } = useParams();

  const {
    data: steps,
    isFetching,
    isError,
    error,
  } = useQuery<Array<StepApiResponse>>({
    queryKey: ["task", taskId, "steps"],
    queryFn: async () => {
      return client
        .get(`/tasks/${taskId}/steps`)
        .then((response) => response.data);
    },
  });

  if (isFetching) {
    return <div>Loading...</div>;
  }

  if (isError) {
    return <div>Error: {error?.message}</div>;
  }

  if (!steps) {
    return <div>No steps found</div>;
  }

  const activeStep = steps[activeIndex];

  return (
    <div className="px-4 flex">
      <aside className="w-64 shrink-0">
        <StepNavigation
          activeIndex={activeIndex}
          onActiveIndexChange={setActiveIndex}
        />
      </aside>
      <main className="px-4 w-full">
        {activeStep ? (
          <StepArtifacts id={activeStep.step_id} stepProps={activeStep} />
        ) : null}
      </main>
    </div>
  );
}

export { StepArtifactsLayout };
