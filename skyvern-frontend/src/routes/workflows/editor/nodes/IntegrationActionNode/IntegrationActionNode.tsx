import {
  Handle,
  NodeProps,
  Position,
  useEdges,
  useNodes,
  useNodesData,
} from "@xyflow/react";
import { useState } from "react";
import { useParams } from "react-router-dom";

import { WorkflowBlockInputTextarea } from "@/components/WorkflowBlockInputTextarea";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { useIntegrationCatalogQuery } from "@/hooks/useIntegrationCatalogQuery";
import { statusIsRunningOrQueued } from "@/routes/tasks/types";
import { useWorkflowRunQuery } from "@/routes/workflows/hooks/useWorkflowRunQuery";
import { cn } from "@/util/utils";

import { useCollapseContext } from "../../collapse/CollapseContext";
import { NodeBody } from "../../collapse/NodeBody";
import { useUpdate } from "../../useUpdate";
import { getAvailableOutputParameterKeys } from "../../workflowEditorUtils";
import { type AppNode } from "..";
import { ParametersMultiSelect } from "../TaskNode/ParametersMultiSelect";
import { BuildModeOnly } from "../BuildModeOnly";
import { NodeHeader } from "../components/NodeHeader";
import { IntegrationAccountPicker } from "./IntegrationAccountPicker";
import {
  editableInputs,
  fieldLabel,
  findIntegrationAction,
  integrationActionTitle,
  isTemplate,
  schemaType,
  type IntegrationActionNode as IntegrationActionNodeType,
  type IntegrationActionNodeData,
  type IntegrationCatalogEntryWithActions,
  type JsonSchema,
} from "./types";
import { entryNeedsAccount } from "./validate";

type FieldProps = {
  nodeId: string;
  name: string;
  schema: JsonSchema;
  value: unknown;
  disabled: boolean;
  required?: boolean;
  onChange: (value: unknown) => void;
};

const UNSET_OPTION = "__unset__";

function toText(value: unknown): string {
  if (value === undefined || value === null) return "";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

function parseJson(text: string): unknown {
  if (!text.trim()) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// Only plain decimals become numbers; anything else (a template, "0x10", a typo) stays text for validation to report.
function parseNumber(text: string): unknown {
  if (!text.trim()) return undefined;
  return /^\s*-?(\d+\.?\d*|\.\d+)\s*$/.test(text) ? Number(text) : text;
}

type TextFieldProps = FieldProps & {
  parse: (text: string) => unknown;
  className: string;
  placeholder?: string;
};

// Local text so "1." or a half-typed object is not rewritten on every keystroke;
// a value changed elsewhere (sidebar, canvas, YAML, undo) replaces it.
function ParsedTextField({
  nodeId,
  name,
  value,
  disabled,
  onChange,
  parse,
  className,
  placeholder,
}: TextFieldProps) {
  const [text, setText] = useState(() => toText(value));
  const [seen, setSeen] = useState(value);
  if (seen !== value) {
    setSeen(value);
    if (JSON.stringify(parse(text)) !== JSON.stringify(value)) {
      setText(toText(value));
    }
  }
  return (
    <WorkflowBlockInputTextarea
      nodeId={nodeId}
      name={name}
      value={text}
      disabled={disabled}
      className={className}
      placeholder={placeholder}
      onChange={(next) => {
        setText(next);
        onChange(parse(next));
      }}
    />
  );
}

function InputField(props: FieldProps) {
  const { nodeId, name, schema, value, disabled, required, onChange } = props;
  const type = schemaType(schema);
  // A templated value would show blank in a dropdown or "off" on a switch, so those show it as text.
  // Number and JSON fields keep their own text field, so typing "{{" does not swap the element mid-edit.
  const hasControl = Boolean(schema.enum) || type === "boolean";
  if (
    (type === "string" && !schema.enum) ||
    (hasControl && isTemplate(value))
  ) {
    return (
      <WorkflowBlockInputTextarea
        nodeId={nodeId}
        name={name}
        value={toText(value)}
        disabled={disabled}
        className={cn(
          "nopan text-xs",
          schema.format === "textarea" && "min-h-24",
        )}
        onChange={(text) => onChange(text === "" ? undefined : text)}
      />
    );
  }
  if (schema.enum) {
    const options = schema.enum.map(String);
    return (
      <Select
        value={value === undefined ? "" : String(value)}
        disabled={disabled}
        onValueChange={(selected) =>
          onChange(
            selected === UNSET_OPTION
              ? undefined
              : schema.enum?.[options.indexOf(selected)],
          )
        }
      >
        <SelectTrigger className="nopan text-xs" aria-label={name}>
          <SelectValue
            placeholder={
              schema.default === undefined || schema.default === null
                ? "Select"
                : `Default: ${String(schema.default)}`
            }
          />
        </SelectTrigger>
        <SelectContent>
          {required ? null : (
            <SelectItem value={UNSET_OPTION}>Not set</SelectItem>
          )}
          {options.map((option) => (
            <SelectItem key={option} value={option}>
              {option}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }
  if (type === "boolean") {
    return (
      <Switch
        aria-label={name}
        // An unset required input saves nothing, so it shows off rather than its default.
        checked={(value ?? (required ? undefined : schema.default)) === true}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    );
  }
  if (type === "number" || type === "integer") {
    return (
      <ParsedTextField
        {...props}
        parse={parseNumber}
        className="nopan text-xs"
      />
    );
  }
  return (
    <ParsedTextField
      {...props}
      parse={parseJson}
      className="nopan min-h-20 font-mono text-xs"
      placeholder={type === "array" ? "[]" : "{}"}
    />
  );
}

function IntegrationActionEditor({ blockId }: { blockId: string }) {
  const nodeSlice = useNodesData<IntegrationActionNodeType>(blockId);
  const data = nodeSlice?.data;
  const nodes = useNodes<AppNode>();
  const edges = useEdges();
  const { data: catalog, isLoading } =
    useIntegrationCatalogQuery<IntegrationCatalogEntryWithActions>();
  const update = useUpdate<IntegrationActionNodeData>({
    id: blockId,
    editable: data?.editable ?? false,
  });
  if (!data) return null;
  const found = findIntegrationAction(
    catalog,
    data.integrationId,
    data.actionId,
  );
  if (!found) {
    return isLoading ? null : (
      <p className="text-xs text-muted-foreground">
        This action is not available in your app list.
      </p>
    );
  }
  const { entry, action } = found;
  const required = new Set(action.inputs.required ?? []);
  return (
    <div className="space-y-4">
      {entryNeedsAccount(entry) ? (
        <div className="space-y-2">
          <Label className="text-xs text-slate-300">Account</Label>
          <IntegrationAccountPicker
            nodeId={blockId}
            entry={entry}
            actionKey={`${entry.id}.${action.id}`}
            value={data.connectionId}
            disabled={!data.editable}
            onChange={(connectionId) => update({ connectionId })}
          />
        </div>
      ) : null}
      {editableInputs(action.inputs).map(([key, schema]) => (
        <div key={`${blockId}:${key}`} className="space-y-2">
          <Label className="text-xs text-slate-300">
            {fieldLabel(key, schema)}
            {required.has(key) ? " *" : ""}
          </Label>
          {schema.description ? (
            <p className="text-xs text-muted-foreground">
              {schema.description}
            </p>
          ) : null}
          <InputField
            nodeId={blockId}
            name={key}
            schema={schema}
            value={data.inputs[key]}
            disabled={!data.editable}
            required={required.has(key)}
            onChange={(value) => {
              const inputs = { ...data.inputs };
              if (value === undefined) delete inputs[key];
              else inputs[key] = value;
              update({ inputs });
            }}
          />
        </div>
      ))}
      <div className="space-y-2">
        <Label className="text-xs text-slate-300">Parameter bindings</Label>
        <ParametersMultiSelect
          availableOutputParameters={getAvailableOutputParameterKeys(
            nodes,
            edges,
            blockId,
          )}
          parameters={data.parameterKeys}
          onParametersChange={(parameterKeys) => update({ parameterKeys })}
          disabled={!data.editable}
        />
      </div>
    </div>
  );
}

function IntegrationActionNode({
  id,
  data,
}: NodeProps<IntegrationActionNodeType>) {
  const { editable, label } = data;
  const { blockLabel: urlBlockLabel } = useParams();
  const { data: workflowRun } = useWorkflowRunQuery();
  const { data: catalog } =
    useIntegrationCatalogQuery<IntegrationCatalogEntryWithActions>();
  const found = findIntegrationAction(
    catalog,
    data.integrationId,
    data.actionId,
  );
  const thisBlockIsTargetted =
    urlBlockLabel !== undefined && urlBlockLabel === label;
  const thisBlockIsPlaying =
    workflowRun && statusIsRunningOrQueued(workflowRun) && thisBlockIsTargetted;
  const { open } = useCollapseContext();

  return (
    <div>
      <Handle
        type="source"
        position={Position.Bottom}
        id="a"
        className="opacity-0"
      />
      <Handle
        type="target"
        position={Position.Top}
        id="b"
        className="opacity-0"
      />
      <div
        className={cn(
          "transform-origin-center w-[30rem] space-y-4 rounded-lg bg-slate-elevation3 px-6 py-4 transition-shadow motion-reduce:transition-none",
          open ? "shadow-md" : "shadow-sm",
          {
            "pointer-events-none": thisBlockIsPlaying,
            "bg-background outline outline-2 outline-ring":
              thisBlockIsTargetted,
          },
        )}
      >
        <NodeHeader
          blockLabel={label}
          blockTitle={
            found
              ? integrationActionTitle(found.entry, found.action)
              : `${data.integrationId}: ${data.actionId}`
          }
          editable={editable}
          nodeId={id}
          totpIdentifier={null}
          totpUrl={null}
          type="integration_action"
        />
        <NodeBody>
          <BuildModeOnly renderInReadOnlyComparison={false}>
            <IntegrationActionEditor blockId={id} />
          </BuildModeOnly>
        </NodeBody>
      </div>
    </div>
  );
}

export { InputField, IntegrationActionEditor, IntegrationActionNode };
