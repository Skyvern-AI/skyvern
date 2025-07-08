import { useLocation } from "react-router-dom";
import type { WorkflowParameter } from "./types/workflowTypes";

type Location = ReturnType<typeof useLocation>;

export const getInitialValues = (
  location: Location,
  workflowParameters: WorkflowParameter[],
) => {
  const iv = location.state?.data
    ? location.state.data
    : workflowParameters?.reduce(
        (acc, curr) => {
          if (curr.workflow_parameter_type === "json") {
            if (typeof curr.default_value === "string") {
              acc[curr.key] = curr.default_value;
              return acc;
            }
            if (curr.default_value) {
              acc[curr.key] = JSON.stringify(curr.default_value, null, 2);
              return acc;
            }
          }
          if (
            curr.default_value &&
            curr.workflow_parameter_type === "boolean"
          ) {
            acc[curr.key] = Boolean(curr.default_value);
            return acc;
          }
          if (
            curr.default_value === null &&
            curr.workflow_parameter_type === "string"
          ) {
            acc[curr.key] = "";
            return acc;
          }
          if (curr.default_value) {
            acc[curr.key] = curr.default_value;
            return acc;
          }
          acc[curr.key] = null;
          return acc;
        },
        {} as Record<string, unknown>,
      );

  return iv as Record<string, unknown>;
};
