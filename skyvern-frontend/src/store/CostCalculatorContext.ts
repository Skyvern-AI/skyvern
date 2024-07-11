import { StepApiResponse } from "@/api/types";
import { createContext } from "react";

type TaskCostCalculator = (steps: Array<StepApiResponse>) => number;

const CostCalculatorContext = createContext<TaskCostCalculator | null>(null);

export { CostCalculatorContext };
