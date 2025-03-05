import { cn } from "@/util/utils";

type Option = {
  label: string;
  value: string;
};

type Props = {
  options: Option[];
  value: string;
  onChange: (value: string) => void;
};

function SwitchBar({ options, value, onChange }: Props) {
  return (
    <div className="flex w-fit gap-1 rounded-sm border border-slate-700 p-2">
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <div
            key={option.value}
            className={cn(
              "cursor-pointer whitespace-nowrap rounded-sm px-3 py-2 text-xs hover:bg-slate-700",
              {
                "bg-slate-700": selected,
              },
            )}
            onClick={() => {
              if (!selected) {
                onChange(option.value);
              }
            }}
          >
            {option.label}
          </div>
        );
      })}
    </div>
  );
}

export { SwitchBar };
