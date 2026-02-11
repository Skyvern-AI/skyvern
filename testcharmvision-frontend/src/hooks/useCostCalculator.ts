import { CostCalculatorContext } from "@/store/CostCalculatorContext";
import { useContext } from "react";

function useCostCalculator() {
  const costCalculator = useContext(CostCalculatorContext);
  return costCalculator;
}

export { useCostCalculator };
