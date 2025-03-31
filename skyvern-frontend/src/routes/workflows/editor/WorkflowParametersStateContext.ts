import { createContext } from "react";
import { ParametersState } from "./types";

type WorkflowParametersState = [
  ParametersState,
  React.Dispatch<React.SetStateAction<ParametersState>>,
];

const WorkflowParametersStateContext = createContext<
  WorkflowParametersState | undefined
>(undefined);

export { WorkflowParametersStateContext };
