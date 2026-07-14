import { Badge } from "@/components/ui/badge";
import { FormControl, FormItem, FormLabel } from "@/components/ui/form";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

import { getRotatingCredentialIds } from "../runWorkflowCredentials";
import { CredentialParameter } from "../types/workflowTypes";

type RotatingCredentialFieldProps = {
  parameter: CredentialParameter;
  value: unknown;
  onChange: (value: string | null) => void;
  credentialNamesById: Map<string, string>;
  title: string;
  description: string;
};

function formatCredentialSelectionStrategy(
  selectionStrategy: CredentialParameter["selection_strategy"],
) {
  if (selectionStrategy === "random") {
    return "Random";
  }

  return "Round robin";
}

function RotatingCredentialField({
  parameter,
  value,
  onChange,
  credentialNamesById,
  title,
  description,
}: RotatingCredentialFieldProps) {
  const credentialIds = getRotatingCredentialIds(parameter);
  const forcedCredentialId = typeof value === "string" ? value : "";
  const mode = forcedCredentialId ? "force" : "rotation";
  const forceSelectValue = forcedCredentialId || credentialIds[0] || "";

  return (
    <FormItem>
      <div className="flex gap-16">
        <FormLabel className="!text-foreground">
          <div className="w-72">
            <div className="flex items-center gap-2 text-lg">
              {title}
              <span className="text-sm text-muted-foreground">
                credential rotation
              </span>
            </div>
            <h2 className="text-sm text-muted-foreground">{description}</h2>
          </div>
        </FormLabel>
        <div className="w-full space-y-3">
          <FormControl>
            <RadioGroup
              value={mode}
              onValueChange={(nextMode) => {
                if (nextMode === "force") {
                  onChange(forceSelectValue);
                } else {
                  onChange(null);
                }
              }}
              className="gap-3"
            >
              <label
                className="flex cursor-pointer items-start gap-3 rounded-md border border-border bg-slate-elevation1/40 p-3"
                htmlFor={`${parameter.key}-rotation`}
              >
                <RadioGroupItem
                  id={`${parameter.key}-rotation`}
                  value="rotation"
                  className="mt-1"
                />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">
                      Use configured rotation
                    </span>
                    <Badge variant="outline" className="text-xs font-normal">
                      {formatCredentialSelectionStrategy(
                        parameter.selection_strategy,
                      )}
                    </Badge>
                  </div>
                  <div className="grid gap-2 md:grid-cols-2">
                    {credentialIds.map((credentialId) => (
                      <div
                        key={credentialId}
                        className="min-w-0 rounded border border-border/60 bg-background/40 px-2 py-1.5 text-xs text-foreground dark:text-slate-200"
                      >
                        <span className="block truncate">
                          {credentialNamesById.get(credentialId) ??
                            credentialId}
                        </span>
                      </div>
                    ))}
                  </div>
                </div>
              </label>

              <label
                className="flex cursor-pointer items-start gap-3 rounded-md border border-border bg-slate-elevation1/40 p-3"
                htmlFor={`${parameter.key}-force`}
              >
                <RadioGroupItem
                  id={`${parameter.key}-force`}
                  value="force"
                  className="mt-1"
                />
                <div className="min-w-0 flex-1 space-y-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-sm font-medium text-foreground">
                      Force one credential for this run
                    </span>
                    {mode === "force" && (
                      <Badge variant="outline" className="text-xs font-normal">
                        Run only
                      </Badge>
                    )}
                  </div>
                  <Select
                    disabled={mode !== "force"}
                    value={forceSelectValue}
                    onValueChange={(value) => onChange(value)}
                  >
                    <SelectTrigger className="w-full">
                      <SelectValue placeholder="Select a credential" />
                    </SelectTrigger>
                    <SelectContent>
                      {credentialIds.map((credentialId) => (
                        <SelectItem key={credentialId} value={credentialId}>
                          {credentialNamesById.get(credentialId) ??
                            credentialId}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </label>
            </RadioGroup>
          </FormControl>
        </div>
      </div>
    </FormItem>
  );
}

export { RotatingCredentialField };
