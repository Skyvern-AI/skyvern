import { useContext } from "react";
import { TaskParametersStateContext } from "./TaskParametersStateContext";

function useTaskParametersState() {
  const value = useContext(TaskParametersStateContext);
  if (value === undefined) {
    throw new Error(
      "useTaskParametersState must be used within a TaskParametersStateProvider",
    );
  }
  return value;
}

export { useTaskParametersState }; 