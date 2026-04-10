import { CalendarIcon, ChevronDownIcon } from "@radix-ui/react-icons";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Checkbox } from "./ui/checkbox";
import { TriggerType } from "@/api/types";

type TriggerTypeDropdownItem = {
  label: string;
  value: TriggerType;
};

const triggerTypeDropdownItems: Array<TriggerTypeDropdownItem> = [
  {
    label: "Manual",
    value: TriggerType.Manual,
  },
  {
    label: "API",
    value: TriggerType.Api,
  },
  {
    label: "Scheduled",
    value: TriggerType.Scheduled,
  },
];

type Props = {
  values: Array<TriggerType>;
  onChange: (values: Array<TriggerType>) => void;
};

function TriggerTypeFilterDropdown({ values, onChange }: Props) {
  const label =
    values.length === 0
      ? "All Runs"
      : values.length === 1
        ? (triggerTypeDropdownItems.find((i) => i.value === values[0])?.label ??
          "All Runs")
        : `${values.length} types`;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          <CalendarIcon className="mr-2 size-4" />
          {label}
          <ChevronDownIcon className="ml-2" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {triggerTypeDropdownItems.map((item) => {
          return (
            <div
              key={item.value}
              className="flex items-center gap-2 p-2 text-sm"
            >
              <Checkbox
                id={`trigger-${item.value}`}
                checked={values.includes(item.value)}
                onCheckedChange={(checked) => {
                  if (checked) {
                    onChange([...values, item.value]);
                  } else {
                    onChange(values.filter((value) => value !== item.value));
                  }
                }}
              />
              <label htmlFor={`trigger-${item.value}`}>{item.label}</label>
            </div>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { TriggerTypeFilterDropdown };
