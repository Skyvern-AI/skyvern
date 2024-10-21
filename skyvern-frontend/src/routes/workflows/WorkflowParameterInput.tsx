import { WorkflowParameterValueType } from "@/api/types";
import { FileInputValue, FileUpload } from "@/components/FileUpload";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { CodeEditor } from "./components/CodeEditor";
import { AutoResizingTextarea } from "@/components/AutoResizingTextarea/AutoResizingTextarea";
import { Label } from "@/components/ui/label";

type Props = {
  type: WorkflowParameterValueType;
  value: unknown;
  onChange: (value: unknown) => void;
};

function WorkflowParameterInput({ type, value, onChange }: Props) {
  if (type === "json") {
    return (
      <CodeEditor
        language="json"
        onChange={(value) => onChange(value)}
        value={
          typeof value === "string" ? value : JSON.stringify(value, null, 2)
        }
      />
    );
  }

  if (type === "string") {
    return (
      <AutoResizingTextarea
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  if (type === "integer") {
    return (
      <Input
        value={value as number}
        onChange={(e) => onChange(parseInt(e.target.value))}
        type="number"
      />
    );
  }

  if (type === "float") {
    return (
      <Input
        value={value as number}
        onChange={(e) => onChange(parseFloat(e.target.value))}
        type="number"
        step="any"
      />
    );
  }

  if (type === "boolean") {
    const checked = typeof value === "boolean" ? value : Boolean(value);
    return (
      <div className="flex items-center gap-2">
        <Checkbox
          checked={checked}
          onCheckedChange={(checked) => onChange(checked)}
          className="block"
        />
        <Label>{value ? "True" : "False"}</Label>
      </div>
    );
  }

  if (type === "file_url") {
    return (
      <FileUpload
        value={value as FileInputValue}
        onChange={(value) => onChange(value)}
      />
    );
  }
}

export { WorkflowParameterInput };
