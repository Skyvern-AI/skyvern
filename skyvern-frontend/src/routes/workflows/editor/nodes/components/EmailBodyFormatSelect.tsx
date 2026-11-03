import { HelpTooltip } from "@/components/HelpTooltip";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { EmailBodyFormat } from "@/routes/workflows/types/workflowTypes";

const bodyFormatOptions: Array<{ value: EmailBodyFormat; label: string }> = [
  { value: "text", label: "Text" },
  { value: "html", label: "HTML" },
];

const bodyFormatTooltip =
  "Text sends the body exactly as written. HTML renders it as a formatted email and includes a plain-text version for clients that cannot show HTML.";

type Props = {
  value: EmailBodyFormat;
  onChange: (value: EmailBodyFormat) => void;
  disabled?: boolean;
};

function EmailBodyFormatSelect({ value, onChange, disabled }: Props) {
  return (
    <div className="flex items-center gap-2">
      <Label className="text-xs text-tertiary-foreground">Format</Label>
      <HelpTooltip content={bodyFormatTooltip} />
      <Select
        value={value}
        onValueChange={(next) => onChange(next as EmailBodyFormat)}
        disabled={disabled}
      >
        <SelectTrigger
          aria-label="Body format"
          className="nopan h-7 w-20 text-xs"
        >
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {bodyFormatOptions.map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export { EmailBodyFormatSelect };
