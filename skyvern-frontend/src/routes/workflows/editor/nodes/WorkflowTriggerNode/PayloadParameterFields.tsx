import { useCallback, useRef } from "react";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { WorkflowParameter } from "@/routes/workflows/types/workflowTypes";
import { Skeleton } from "@/components/ui/skeleton";

interface PayloadParameterFieldsProps {
  parameters: Array<WorkflowParameter>;
  payload: string;
  onChange: (payload: string) => void;
  nodeId: string;
  isLoading: boolean;
}

function parsePayload(payload: string): Record<string, string> {
  try {
    const parsed = JSON.parse(payload);
    if (typeof parsed === "object" && parsed !== null) {
      const result: Record<string, string> = {};
      for (const [key, val] of Object.entries(parsed)) {
        result[key] = typeof val === "string" ? val : JSON.stringify(val);
      }
      return result;
    }
  } catch {
    // ignore parse errors
  }
  return {};
}

function PayloadParameterFields({
  parameters,
  payload,
  onChange,
  nodeId,
  isLoading,
}: PayloadParameterFieldsProps) {
  const payloadRef = useRef(payload);
  payloadRef.current = payload;

  const handleFieldChange = useCallback(
    (key: string, value: string) => {
      const currentPayload = parsePayload(payloadRef.current);
      if (value === "") {
        delete currentPayload[key];
      } else {
        currentPayload[key] = value;
      }
      const newPayload = JSON.stringify(currentPayload, null, 2);
      payloadRef.current = newPayload;
      onChange(newPayload);
    },
    [onChange],
  );

  if (isLoading) {
    return (
      <div className="space-y-3">
        {Array.from({ length: 2 }).map((_, i) => (
          <div key={i} className="space-y-1.5">
            <Skeleton className="h-3.5 w-24" />
            <Skeleton className="h-8 w-full" />
          </div>
        ))}
      </div>
    );
  }

  if (parameters.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        This workflow has no input parameters.
      </p>
    );
  }

  const payloadValues = parsePayload(payload);

  return (
    <div className="space-y-3">
      {parameters.map((param) => (
        <div key={param.key} className="space-y-1.5">
          <div className="flex items-baseline gap-2">
            <Label className="text-xs text-slate-300">{param.key}</Label>
            <span className="text-[10px] text-slate-500">
              {param.workflow_parameter_type}
            </span>
          </div>
          {param.description && (
            <p className="text-[10px] text-slate-500">{param.description}</p>
          )}
          <WorkflowBlockInputTextarea
            nodeId={nodeId}
            onChange={(val) => handleFieldChange(param.key, val)}
            value={payloadValues[param.key] ?? ""}
            placeholder={
              param.default_value != null
                ? `Default: ${String(param.default_value)}`
                : `Enter ${param.key}...`
            }
            className="nopan text-xs"
          />
        </div>
      ))}
    </div>
  );
}

export { PayloadParameterFields };
