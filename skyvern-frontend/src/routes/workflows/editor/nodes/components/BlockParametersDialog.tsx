import { useState, useEffect, useMemo } from "react";
import { ReloadIcon } from "@radix-ui/react-icons";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { WorkflowParameterInput } from "@/routes/workflows/WorkflowParameterInput";
import { getLabelForWorkflowParameterType } from "@/routes/workflows/editor/workflowEditorUtils";
import type { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";

/**
 * Validates a parameter value based on its type.
 * Matches the validation logic in RunWorkflowForm.
 */
function validateParameterValue(value: unknown, type: string): string | null {
  switch (type) {
    case "json":
      if (value === null || value === undefined) {
        return "This field is required";
      }
      if (typeof value === "string") {
        const trimmed = value.trim();
        if (trimmed === "") {
          return "This field is required";
        }
        try {
          JSON.parse(trimmed);
          return null;
        } catch (e) {
          const message = e instanceof SyntaxError ? e.message : "Parse error";
          return `Invalid JSON: ${message}`;
        }
      }
      return null;

    case "boolean":
      if (value === null || value === undefined) {
        return "This field is required";
      }
      return null;

    case "integer":
    case "float":
      if (value === null || value === undefined || Number.isNaN(value)) {
        return "This field is required";
      }
      return null;

    case "file_url":
      if (
        value === null ||
        value === undefined ||
        (typeof value === "string" && value.trim() === "") ||
        (typeof value === "object" &&
          value !== null &&
          "s3uri" in value &&
          !(value as { s3uri: unknown }).s3uri)
      ) {
        return "This field is required";
      }
      return null;

    default:
      return null;
  }
}

interface BlockParametersDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  blockLabel: string;
  parameters: WorkflowParameter[];
  initialValues: Record<string, unknown>;
  onSubmit: (values: Record<string, unknown>) => void;
  isLoading?: boolean;
}

function getDefaultValues(
  parameters: WorkflowParameter[],
  initialValues: Record<string, unknown>,
): Record<string, unknown> {
  const values: Record<string, unknown> = { ...initialValues };
  for (const param of parameters) {
    if (values[param.key] === undefined || values[param.key] === null) {
      // Set defaults - use null for required types to force user selection
      // This matches RunWorkflowForm behavior
      switch (param.workflow_parameter_type) {
        case "string":
          values[param.key] = "";
          break;
        case "integer":
        case "float":
        case "boolean":
        case "file_url":
          values[param.key] = null;
          break;
        case "json":
          values[param.key] = "";
          break;
        default:
          values[param.key] = null;
      }
    }
  }
  return values;
}

function BlockParametersDialog({
  open,
  onOpenChange,
  blockLabel,
  parameters,
  initialValues,
  onSubmit,
  isLoading = false,
}: BlockParametersDialogProps) {
  const [values, setValues] = useState<Record<string, unknown>>({});

  // Reset values when dialog opens
  useEffect(() => {
    if (open) {
      setValues(getDefaultValues(parameters, initialValues));
    }
    // Only reset when dialog opens - don't react to parameter/initialValues changes while open
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Validate all parameters matching RunWorkflowForm validation
  const validationErrors = useMemo(() => {
    const errors: Record<string, string> = {};
    for (const param of parameters) {
      const error = validateParameterValue(
        values[param.key],
        param.workflow_parameter_type,
      );
      if (error) {
        errors[param.key] = error;
      }
    }
    return errors;
  }, [parameters, values]);

  const hasValidationErrors = Object.keys(validationErrors).length > 0;

  const handleValueChange = (key: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [key]: value }));
  };

  const handleSubmit = () => {
    if (hasValidationErrors) {
      return;
    }
    // Merge with initial values to include all parameters
    const mergedValues = { ...initialValues, ...values };
    onSubmit(mergedValues);
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>Enter Parameter Values</DialogTitle>
          <DialogDescription>
            The block "{blockLabel}" requires the following parameters to run.
          </DialogDescription>
        </DialogHeader>

        <ScrollArea>
          {/* Height set to ~3.5 parameters to create visual cutoff hint */}
          <ScrollAreaViewport className="max-h-[340px]">
            {/* px-1 prevents focus ring from being clipped on left/right edges */}
            <div className="space-y-6 px-1 py-2 pr-4">
              {parameters.map((param) => (
                <div key={param.key} className="space-y-2">
                  <div className="flex items-baseline gap-2">
                    <Label
                      htmlFor={param.key}
                      className="text-base font-medium"
                    >
                      {param.key}
                    </Label>
                    <span className="text-sm text-slate-400">
                      {getLabelForWorkflowParameterType(
                        param.workflow_parameter_type,
                      )}
                    </span>
                  </div>
                  {param.description && (
                    <p className="text-sm text-slate-400">
                      {param.description}
                    </p>
                  )}
                  <div className="pt-1">
                    <WorkflowParameterInput
                      type={param.workflow_parameter_type}
                      value={values[param.key]}
                      onChange={(value) => handleValueChange(param.key, value)}
                    />
                    {validationErrors[param.key] && (
                      <p className="mt-1 text-sm text-destructive">
                        {validationErrors[param.key]}
                      </p>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </ScrollAreaViewport>
        </ScrollArea>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button
            variant="secondary"
            onClick={() => onOpenChange(false)}
            disabled={isLoading}
          >
            Cancel
          </Button>
          <Button
            onClick={handleSubmit}
            disabled={isLoading || hasValidationErrors}
          >
            {isLoading ? (
              <>
                <ReloadIcon className="mr-2 h-4 w-4 animate-spin" />
                Running...
              </>
            ) : (
              "Run Block"
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { BlockParametersDialog };
