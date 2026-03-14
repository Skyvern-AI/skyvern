import { useCallback, useRef, useState } from "react";
import { Label } from "@/components/ui/label";
import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import {
  WorkflowParameter,
  WorkflowParameterValueType,
} from "@/routes/workflows/types/workflowTypes";
import { Skeleton } from "@/components/ui/skeleton";
import { CredentialSelector } from "../../../components/CredentialSelector";
import { useCredentialsQuery } from "../../../hooks/useCredentialsQuery";

interface PayloadParameterFieldsProps {
  parameters: Array<WorkflowParameter>;
  payload: string;
  onChange: (payload: string) => void;
  nodeId: string;
  isLoading: boolean;
}

function getDynamicInputPlaceholder(
  param: WorkflowParameter,
  isCredential: boolean,
): string {
  if (isCredential) return "e.g. {{ credential }}";
  if (param.default_value != null)
    return `Default: ${String(param.default_value)}`;
  return `Enter ${param.key}...`;
}

function isCredentialParam(param: WorkflowParameter): boolean {
  return (
    param.workflow_parameter_type === WorkflowParameterValueType.CredentialId
  );
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
  const { data: credentials } = useCredentialsQuery({ page_size: 100 });
  const credentialNameById = new Map(
    credentials?.map((c) => [c.credential_id, c.name]),
  );

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

  const [dynamicCredentialKeys, setDynamicCredentialKeys] = useState<
    Set<string>
  >(() => {
    const currentValues = parsePayload(payload);
    return new Set(
      parameters
        .filter((p) => isCredentialParam(p))
        .filter((p) => {
          const v = currentValues[p.key] ?? "";
          return v !== "" && !v.startsWith("cred_");
        })
        .map((p) => p.key),
    );
  });

  const toggleDynamicCredential = useCallback(
    (key: string) => {
      setDynamicCredentialKeys((prev) => {
        const next = new Set(prev);
        if (next.has(key)) {
          next.delete(key);
          handleFieldChange(key, "");
        } else {
          next.add(key);
        }
        return next;
      });
    },
    [handleFieldChange],
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
      {parameters.map((param) => {
        const isCredential = isCredentialParam(param);
        const isDynamic = dynamicCredentialKeys.has(param.key);

        return (
          <div key={param.key} className="space-y-1.5">
            <div className="flex items-center justify-between">
              <div className="flex items-baseline gap-2">
                <Label className="text-xs text-slate-300">{param.key}</Label>
                <span className="text-[10px] text-slate-500">
                  {param.workflow_parameter_type}
                </span>
              </div>
              {isCredential && (
                <button
                  type="button"
                  className="text-xs text-blue-400 hover:underline"
                  onClick={() => toggleDynamicCredential(param.key)}
                >
                  {isDynamic ? "Use selector" : "Use dynamic value"}
                </button>
              )}
            </div>
            {param.description && (
              <p className="text-[10px] text-slate-500">{param.description}</p>
            )}
            {isCredential && !isDynamic ? (
              <CredentialSelector
                value={payloadValues[param.key] ?? ""}
                onChange={(val) => handleFieldChange(param.key, val)}
                placeholder={(() => {
                  if (param.default_value == null) return "Select a credential";
                  const defaultId = String(param.default_value);
                  const name = credentialNameById.get(defaultId);
                  return name ? `Default: ${name}` : `Default: ${defaultId}`;
                })()}
              />
            ) : (
              <>
                <WorkflowBlockInputTextarea
                  nodeId={nodeId}
                  onChange={(val) => handleFieldChange(param.key, val)}
                  value={payloadValues[param.key] ?? ""}
                  placeholder={getDynamicInputPlaceholder(param, isCredential)}
                  className="nopan text-xs"
                />
                {isCredential &&
                  !payloadValues[param.key] &&
                  param.default_value != null && (
                    <p className="text-[10px] text-slate-500">
                      Default:{" "}
                      {credentialNameById.get(String(param.default_value)) ??
                        String(param.default_value)}
                    </p>
                  )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

export { PayloadParameterFields };
