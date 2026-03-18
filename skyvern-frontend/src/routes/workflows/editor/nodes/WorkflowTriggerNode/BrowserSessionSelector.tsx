import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  PARENT_SESSION_VALUE,
  FRESH_SESSION_VALUE,
} from "./browserSessionConstants";

type Props = {
  value: string;
  onChange: (value: string) => void;
  waitForCompletion: boolean;
};

function BrowserSessionSelector({ value, onChange, waitForCompletion }: Props) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-full text-xs">
        <SelectValue placeholder="Select browser session" />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectItem
            value={PARENT_SESSION_VALUE}
            disabled={!waitForCompletion}
            className="text-xs"
          >
            Continue in the same session
          </SelectItem>
          <SelectItem value={FRESH_SESSION_VALUE} className="text-xs">
            Create a new browser
          </SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  );
}

export { BrowserSessionSelector };
