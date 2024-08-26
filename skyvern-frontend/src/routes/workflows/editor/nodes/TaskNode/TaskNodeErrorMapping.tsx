import { Checkbox } from "@/components/ui/checkbox";
import { Label } from "@/components/ui/label";
import { CodeEditor } from "@/routes/workflows/components/CodeEditor";

type Props = {
  value: Record<string, unknown> | null;
  onChange: (value: Record<string, unknown> | null) => void;
  disabled?: boolean;
};

function TaskNodeErrorMapping({ value, onChange, disabled }: Props) {
  if (value === null) {
    return (
      <div className="flex gap-2">
        <Label className="text-xs font-normal text-slate-300">
          Error Messages
        </Label>
        <Checkbox
          checked={false}
          disabled={disabled}
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
        <Label className="text-xs font-normal text-slate-300">
          Error Messages
        </Label>
        <Checkbox
          checked
          disabled={disabled}
          onCheckedChange={() => {
            onChange(null);
          }}
        />
      </div>
      <div>
        <CodeEditor
          language="json"
          value={JSON.stringify(value, null, 2)}
          disabled={disabled}
          onChange={() => {
            if (disabled) {
              return;
            }
            // TODO
          }}
          className="nowheel nopan"
        />
      </div>
    </div>
  );
}

export { TaskNodeErrorMapping };
