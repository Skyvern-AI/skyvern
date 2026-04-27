import { FileInputValue, FileUpload } from "@/components/FileUpload";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CodeEditor } from "./components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { WorkflowParameterValueType } from "./types/workflowTypes";
import { CredentialSelector } from "./components/CredentialSelector";

type Props = {
  type: WorkflowParameterValueType;
  value: unknown;
  onChange: (value: unknown) => void;
  required?: boolean;
  disabled?: boolean;
};

function WorkflowParameterInput({
  type,
  value,
  onChange,
  required,
  disabled,
}: Props) {
  if (type === "json") {
    return (
      <CodeEditor
        className="w-full"
        language="json"
        aria-required={required || undefined}
        readOnly={disabled}
        onChange={(value) => onChange(value)}
        value={
          typeof value === "string" ? value : JSON.stringify(value, null, 2)
        }
        minHeight="96px"
        maxHeight="500px"
      />
    );
  }

  if (type === "string") {
    return (
      <AutoResizingTextarea
        aria-required={required || undefined}
        disabled={disabled}
        value={(value as string | null | undefined) ?? ""}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  if (type === "integer") {
    return (
      <Input
        aria-required={required || undefined}
        disabled={disabled}
        value={value === null ? "" : Number(value)}
        onChange={(e) => {
          const val = e.target.value;
          // Return null for empty input, otherwise parse as integer
          onChange(val === "" ? null : parseInt(val, 10));
        }}
        type="number"
      />
    );
  }

  if (type === "float") {
    return (
      <Input
        aria-required={required || undefined}
        disabled={disabled}
        value={value === null ? "" : Number(value)}
        onChange={(e) => {
          const val = e.target.value;
          // Return null for empty input, otherwise parse as float
          onChange(val === "" ? null : parseFloat(val));
        }}
        type="number"
        step="any"
      />
    );
  }

  if (type === "boolean") {
    return (
      <Select
        disabled={disabled}
        value={value === null ? "" : String(value)}
        onValueChange={(v) => onChange(v === "true")}
      >
        <SelectTrigger aria-required={required || undefined} className="w-48">
          <SelectValue placeholder="Select value..." />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="true">True</SelectItem>
          <SelectItem value="false">False</SelectItem>
        </SelectContent>
      </Select>
    );
  }

  if (type === "file_url") {
    return (
      <FileUpload
        required={required}
        value={value as FileInputValue}
        onChange={(value) => onChange(value)}
      />
    );
  }

  if (type === "credential_id") {
    const credentialId = value as string | null;
    return (
      <CredentialSelector
        required={required}
        value={credentialId ?? ""}
        onChange={(value) => onChange(value)}
      />
    );
  }

  return null;
}

export { WorkflowParameterInput };
