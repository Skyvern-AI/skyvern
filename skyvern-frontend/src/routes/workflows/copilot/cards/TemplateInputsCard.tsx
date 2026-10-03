import { MixerVerticalIcon } from "@radix-ui/react-icons";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useWorkflowHasChangesStore } from "@/store/WorkflowHasChangesStore";
import { useWorkflowParametersStore } from "@/store/WorkflowParametersStore";
import { refuseMutationDuringYamlCommit } from "@/store/WorkflowYamlEditorStore";

import { CredentialsModal } from "@/routes/credentials/CredentialsModal";

import { useSaveWorkflow } from "../../editor/hooks/useSaveWorkflow";
import { CredentialCombobox } from "../../components/CredentialCombobox";
import { parameterIsSkyvernCredential } from "../../editor/types";
import { WorkflowParameterValueType } from "../../types/workflowTypes";
import { CardHeader, CardPill, CopilotCard } from "./cardChrome";

const TEXT_TYPES: ReadonlySet<string> = new Set([
  WorkflowParameterValueType.String,
  WorkflowParameterValueType.Integer,
  WorkflowParameterValueType.Float,
  WorkflowParameterValueType.FileURL,
]);

function numericError(dataType: string, value: string): string | null {
  const trimmed = value.trim();
  if (trimmed === "") return null;
  if (dataType === WorkflowParameterValueType.Integer) {
    return /^-?\d+$/.test(trimmed) ? null : "Enter a whole number";
  }
  if (dataType === WorkflowParameterValueType.Float) {
    return /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/.test(trimmed)
      ? null
      : "Enter a number";
  }
  return null;
}

function TemplateInputsCard() {
  const { parameters, setParametersFromUser } = useWorkflowParametersStore();
  const setHasChanges = useWorkflowHasChangesStore(
    (state) => state.setHasChanges,
  );
  const [edits, setEdits] = useState<Record<string, string>>({});
  const onSave = useSaveWorkflow();
  const [saved, setSaved] = useState<"saved" | "applied" | null>(null);
  const [saving, setSaving] = useState(false);
  const [creatingFor, setCreatingFor] = useState<string | null>(null);

  const inputs = parameters.filter(
    (parameter) =>
      parameter.parameterType === "workflow" ||
      parameter.parameterType === "credential",
  );
  if (inputs.length === 0) return null;

  const hasInvalidEdit = inputs.some(
    (parameter) =>
      parameter.parameterType === "workflow" &&
      parameter.key in edits &&
      numericError(parameter.dataType, edits[parameter.key] ?? "") !== null,
  );

  const setEdit = (key: string, value: string) => {
    setSaved(null);
    setEdits((prev) => ({ ...prev, [key]: value }));
  };

  const save = () => {
    if (hasInvalidEdit || refuseMutationDuringYamlCommit()) return;
    setParametersFromUser(
      parameters.map((parameter) => {
        if (!(parameter.key in edits)) return parameter;
        if (parameter.parameterType === "workflow") {
          const value = edits[parameter.key] ?? "";
          const numeric =
            parameter.dataType === WorkflowParameterValueType.Integer ||
            parameter.dataType === WorkflowParameterValueType.Float;
          return {
            ...parameter,
            defaultValue: numeric && value.trim() === "" ? null : value,
          };
        }
        if (
          parameter.parameterType === "credential" &&
          parameterIsSkyvernCredential(parameter)
        ) {
          // A rotation pool would override the primary the user just chose.
          return {
            ...parameter,
            credentialId: edits[parameter.key] ?? "",
            credentialIds: null,
            selectionStrategy: null,
            fallbackCredentialIds: null,
            fallbackTrigger: null,
          };
        }
        return parameter;
      }),
    );
    setHasChanges(true);
    setEdits({});
    setSaving(true);
    // onSave already toasts a failure; the values stay applied in the editor for a retry via Save.
    onSave()
      .then(() => setSaved("saved"))
      .catch(() => setSaved("applied"))
      .finally(() => setSaving(false));
  };

  return (
    <CopilotCard>
      <CardHeader
        icon={<MixerVerticalIcon className="size-3.5" />}
        title="Inputs"
        meta="fill in before running"
        right={
          saved ? (
            <CardPill tone={saved === "saved" ? "green" : "amber"}>
              {saved === "saved" ? "Saved" : "Not saved"}
            </CardPill>
          ) : null
        }
      />
      <div className="space-y-3 px-3 pb-3">
        {inputs.map((parameter) => {
          const skyvernCredential =
            parameter.parameterType === "credential" &&
            parameterIsSkyvernCredential(parameter)
              ? parameter
              : null;
          const isText =
            parameter.parameterType === "workflow" &&
            TEXT_TYPES.has(parameter.dataType);
          const stored =
            parameter.parameterType === "workflow"
              ? parameter.defaultValue
              : skyvernCredential?.credentialId;
          const current =
            edits[parameter.key] ??
            (stored === null || stored === undefined ? "" : String(stored));
          const error =
            parameter.parameterType === "workflow"
              ? numericError(parameter.dataType, current)
              : null;
          return (
            <div key={parameter.key} className="space-y-1">
              <div className="text-xs font-semibold text-foreground">
                {parameter.key}
              </div>
              {parameter.description ? (
                <div className="text-[11px] text-muted-foreground">
                  {parameter.description}
                </div>
              ) : null}
              {isText ? (
                <>
                  <Input
                    aria-label={parameter.key}
                    value={current}
                    aria-invalid={error !== null}
                    onChange={(event) =>
                      setEdit(parameter.key, event.target.value)
                    }
                  />
                  {error ? (
                    <div className="text-[11px] text-destructive">{error}</div>
                  ) : null}
                </>
              ) : skyvernCredential ? (
                <CredentialCombobox
                  value={current}
                  selectedCredentialId={current || undefined}
                  onValueChange={(value) => setEdit(parameter.key, value)}
                  onAddNew={() => setCreatingFor(parameter.key)}
                />
              ) : (
                <div className="text-[11px] text-muted-foreground">
                  Set this one from Inputs on the start block.
                </div>
              )}
            </div>
          );
        })}
        <Button
          size="sm"
          disabled={Object.keys(edits).length === 0 || saving || hasInvalidEdit}
          onClick={save}
        >
          Save inputs
        </Button>
      </div>
      <CredentialsModal
        isOpen={creatingFor !== null}
        onOpenChange={(open) => {
          if (!open) setCreatingFor(null);
        }}
        onCredentialCreated={(id) => {
          if (creatingFor) setEdit(creatingFor, id);
          setCreatingFor(null);
        }}
      />
    </CopilotCard>
  );
}

export { TemplateInputsCard };
