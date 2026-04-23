import { useCallback, useRef, useState } from "react";
import type { Parameter } from "@/routes/workflows/types/workflowTypes";
import {
  buildInitialParameterValues,
  validateScheduleParameters,
} from "@/routes/workflows/components/scheduleParameters";

type ScheduleParameterValues = Record<string, unknown>;
type ScheduleParameterErrors = Record<string, string | undefined>;
type ScheduleParameterState = {
  values: ScheduleParameterValues;
  errors: ScheduleParameterErrors;
};

function createScheduleParameterState(
  workflowParameters: ReadonlyArray<Parameter>,
  storedValues: Record<string, unknown> | null = null,
): ScheduleParameterState {
  return {
    values: buildInitialParameterValues(workflowParameters, storedValues),
    errors: {},
  };
}

function clearScheduleParameterError(
  errors: ScheduleParameterErrors,
  key: string,
): ScheduleParameterErrors {
  if (!errors[key]) {
    return errors;
  }

  const nextErrors = { ...errors };
  delete nextErrors[key];
  return nextErrors;
}

function applyScheduleParameterChange(
  state: ScheduleParameterState,
  key: string,
  value: unknown,
): ScheduleParameterState {
  return {
    values: { ...state.values, [key]: value },
    errors: clearScheduleParameterError(state.errors, key),
  };
}

function getScheduleParameterValidationResult(
  workflowParameters: ReadonlyArray<Parameter>,
  values: ScheduleParameterValues,
): { errors: ScheduleParameterErrors; isValid: boolean } {
  const errors = validateScheduleParameters(workflowParameters, values);
  return {
    errors,
    isValid: Object.keys(errors).length === 0,
  };
}

function useScheduleParameterState(
  workflowParameters: ReadonlyArray<Parameter>,
  storedValues: Record<string, unknown> | null = null,
) {
  const [values, setValues] = useState<ScheduleParameterValues>(() =>
    buildInitialParameterValues(workflowParameters, storedValues),
  );
  const [errors, setErrors] = useState<ScheduleParameterErrors>({});
  const valuesRef = useRef(values);
  valuesRef.current = values;

  const handleChange = useCallback((key: string, value: unknown) => {
    setValues((prevValues) => {
      const next = { ...prevValues, [key]: value };
      valuesRef.current = next;
      return next;
    });
    setErrors((prevErrors) => clearScheduleParameterError(prevErrors, key));
  }, []);

  const validate = useCallback(() => {
    const result = getScheduleParameterValidationResult(
      workflowParameters,
      valuesRef.current,
    );
    setErrors(result.errors);
    return result.isValid;
  }, [workflowParameters]);

  const reset = useCallback(
    (nextStoredValues: Record<string, unknown> | null = storedValues) => {
      const nextState = createScheduleParameterState(
        workflowParameters,
        nextStoredValues,
      );
      valuesRef.current = nextState.values;
      setValues(nextState.values);
      setErrors(nextState.errors);
    },
    [storedValues, workflowParameters],
  );

  const clear = useCallback(() => {
    valuesRef.current = {};
    setValues({});
    setErrors({});
  }, []);

  return {
    values,
    errors,
    handleChange,
    validate,
    reset,
    clear,
  };
}

export {
  applyScheduleParameterChange,
  clearScheduleParameterError,
  createScheduleParameterState,
  getScheduleParameterValidationResult,
  useScheduleParameterState,
};
