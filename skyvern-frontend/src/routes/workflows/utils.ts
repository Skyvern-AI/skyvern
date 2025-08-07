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

export interface Duration {
  hour: number;
  minute: number;
  second: number;
}

export const toDuration = (seconds: number): Duration => {
  let minutes = Math.floor(seconds / 60);
  let hours = Math.floor(minutes / 60);
  seconds = seconds % 60;
  minutes = minutes % 60;
  hours = hours % 24;

  return {
    hour: Math.floor(hours),
    minute: Math.floor(minutes),
    second: Math.floor(seconds),
  };
};

export const formatDuration = (duration: Duration): string => {
  if (duration.hour) {
    return `${duration.hour}h ${duration.minute}m ${duration.second}s`;
  } else if (duration.minute) {
    return `${duration.minute}m ${duration.second}s`;
  } else {
    return `${duration.second}s`;
  }
};
