import { RunEngine } from "@/api/types";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

type Props = {
  value: RunEngine | null;
  onChange: (value: RunEngine) => void;
  className?: string;
};

function RunEngineSelector({ value, onChange, className }: Props) {
  return (
    <Select value={value ?? RunEngine.SkyvernV1} onValueChange={onChange}>
      <SelectTrigger className={className}>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={RunEngine.SkyvernV1}>Skyvern 1.0</SelectItem>
        <SelectItem value={RunEngine.OpenaiCua}>OpenAI CUA</SelectItem>
        <SelectItem value={RunEngine.AnthropicCua}>Anthropic CUA</SelectItem>
      </SelectContent>
    </Select>
  );
}

export { RunEngineSelector };
