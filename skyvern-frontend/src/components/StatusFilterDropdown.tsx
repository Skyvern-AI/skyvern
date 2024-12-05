import { ChevronDownIcon } from "@radix-ui/react-icons";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Checkbox } from "./ui/checkbox";
import { Status } from "@/api/types";

type Item = {
  label: string;
  value: Status;
};

type Props = {
  options: Array<Item>;
  values: Array<Status>;
  onChange: (values: Array<Status>) => void;
};

function StatusFilterDropdown({ options, values, onChange }: Props) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          Filter by Status <ChevronDownIcon className="ml-2" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {options.map((item) => {
          return (
            <div
              key={item.value}
              className="flex items-center gap-2 p-2 text-sm"
            >
              <Checkbox
                id={item.value}
                checked={values.includes(item.value)}
                onCheckedChange={(checked) => {
                  if (checked) {
                    onChange([...values, item.value]);
                  } else {
                    onChange(values.filter((value) => value !== item.value));
                  }
                }}
              />
              <label htmlFor={item.value}>{item.label}</label>
            </div>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { StatusFilterDropdown };
