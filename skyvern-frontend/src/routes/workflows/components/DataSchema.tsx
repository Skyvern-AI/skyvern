import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { CodeEditor } from "./CodeEditor";

type Props = {
  value: Record<string, unknown> | null;
  onChange: (value: Record<string, unknown> | null) => void;
};

function DataSchema({ value, onChange }: Props) {
  if (value === null) {
    return (
      <div className="flex gap-2">
        <Label className="text-xs text-slate-300">Data Schema</Label>
        <Checkbox
          checked={false}
          onCheckedChange={() => {
            onChange({});
          }}
        />
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="flex gap-2">
        <Label className="text-xs text-slate-300">Data Schema</Label>
        <Checkbox
          checked
          onCheckedChange={() => {
            onChange(null);
          }}
        />
      </div>
      <div>
        <CodeEditor
          language="json"
          value={JSON.stringify(value, null, 2)}
          onChange={() => {
            // TODO
          }}
          className="nopan"
          fontSize={8}
        />
      </div>
    </div>
  );
}

export { DataSchema };
