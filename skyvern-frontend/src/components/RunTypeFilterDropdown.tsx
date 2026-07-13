import { ChevronDownIcon } from "@radix-ui/react-icons";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { Checkbox } from "./ui/checkbox";
import { TaskRunType } from "@/api/types";

// Customer-facing groups over the raw RunType enum; engine-specific CUA
// values are never surfaced individually.
export const RunTypeGroup = {
  Task: "task",
  Workflow: "workflow",
  Agent: "agent",
} as const;

export type RunTypeGroup = (typeof RunTypeGroup)[keyof typeof RunTypeGroup];

// eslint-disable-next-line react-refresh/only-export-components
export const runTypeGroupToRunTypes: Record<
  RunTypeGroup,
  Array<TaskRunType>
> = {
  [RunTypeGroup.Task]: [TaskRunType.TaskV1, TaskRunType.TaskV2],
  [RunTypeGroup.Workflow]: [TaskRunType.WorkflowRun],
  [RunTypeGroup.Agent]: [
    TaskRunType.OpenaiCua,
    TaskRunType.AnthropicCua,
    TaskRunType.UiTars,
    TaskRunType.YutoriNavigator,
  ],
};

const runTypeDropdownItems: Array<{ label: string; value: RunTypeGroup }> = [
  {
    label: "Task",
    value: RunTypeGroup.Task,
  },
  {
    label: "Workflow",
    value: RunTypeGroup.Workflow,
  },
  {
    label: "Agent (CUA)",
    value: RunTypeGroup.Agent,
  },
];

type Props = {
  values: Array<RunTypeGroup>;
  onChange: (values: Array<RunTypeGroup>) => void;
};

function RunTypeFilterDropdown({ values, onChange }: Props) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline">
          Filter by Type <ChevronDownIcon className="ml-2" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {runTypeDropdownItems.map((item) => {
          return (
            <div
              key={item.value}
              className="flex items-center gap-2 p-2 text-sm"
            >
              <Checkbox
                id={`run-type-${item.value}`}
                checked={values.includes(item.value)}
                onCheckedChange={(checked) => {
                  if (checked) {
                    onChange([...values, item.value]);
                  } else {
                    onChange(values.filter((value) => value !== item.value));
                  }
                }}
              />
              <label htmlFor={`run-type-${item.value}`}>{item.label}</label>
            </div>
          );
        })}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

export { RunTypeFilterDropdown };
