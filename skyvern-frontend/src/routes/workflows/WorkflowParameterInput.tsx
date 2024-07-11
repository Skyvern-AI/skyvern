import { WorkflowParameterType } from "@/api/types";
import { FileInputValue, FileUpload } from "@/components/FileUpload";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

type Props = {
  type: WorkflowParameterType;
  value: unknown;
  onChange: (value: unknown) => void;
};

function WorkflowParameterInput({ type, value, onChange }: Props) {
  if (type === "json" || type === "string") {
    return (
      <Textarea
        value={value as string}
        onChange={(e) => onChange(e.target.value)}
        rows={5}
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
    return (
      <Checkbox
        checked={value as boolean}
        onCheckedChange={(checked) => onChange(checked)}
        className="block"
      />
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
