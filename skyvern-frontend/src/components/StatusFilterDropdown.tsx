import { ChevronDownIcon } from "@radix-ui/react-icons";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Checkbox } from "./ui/checkbox";
import { Status } from "@/api/types";

type StatusDropdownItem = {
  label: string;
  value: Status;
};

const statusDropdownItems: Array<StatusDropdownItem> = [
  {
    label: "Completed",
    value: Status.Completed,
  },
  {
    label: "Failed",
    value: Status.Failed,
  },
  {
    label: "Running",
    value: Status.Running,
  },
  {
    label: "Queued",
    value: Status.Queued,
  },
  {
    label: "Terminated",
    value: Status.Terminated,
  },
  {
    label: "Canceled",
    value: Status.Canceled,
  },
  {
    label: "Timed Out",
    value: Status.TimedOut,
  },
  {
    label: "Created",
    value: Status.Created,
  },
];

type Item = {
  label: string;
  value: Status;
};

type Props = {
  values: Array<Status>;
  onChange: (values: Array<Status>) => void;
  options?: Array<Item>;
};

function StatusFilterDropdown({ options, values, onChange }: Props) {
  const dropdownOptions = options ?? statusDropdownItems; // allow options to be overridden by the user of this component

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          Filter by Status <ChevronDownIcon className="ml-2" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {dropdownOptions.map((item) => {
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
