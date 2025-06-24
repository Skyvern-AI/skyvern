import { createContext } from "react";
import { ParametersState } from "../../workflows/editor/types";

type TaskParametersState = [
  ParametersState,
  React.Dispatch<React.SetStateAction<ParametersState>>,
];

const TaskParametersStateContext = createContext<
  TaskParametersState | undefined
>(undefined);

export { TaskParametersStateContext }; 