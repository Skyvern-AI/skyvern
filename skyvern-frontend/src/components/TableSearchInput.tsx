import { MagnifyingGlassIcon } from "@radix-ui/react-icons";
import { Input } from "@/components/ui/input";
import { cn } from "@/util/utils";
import { ChangeEvent } from "react";

type TableSearchInputProps = {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
  inputClassName?: string;
  disabled?: boolean;
  maxLength?: number;
};

function TableSearchInput({
  value,
  onChange,
  placeholder = "Search…",
  className,
  inputClassName,
  disabled,
  maxLength,
}: TableSearchInputProps) {
  function handleChange(event: ChangeEvent<HTMLInputElement>) {
    onChange(event.target.value);
  }

  return (
    <div className={cn("relative", className)}>
      <div className="pointer-events-none absolute left-0 top-0 flex h-9 w-9 items-center justify-center">
        <MagnifyingGlassIcon className="size-5 text-slate-400" />
      </div>
      <Input
        value={value}
        onChange={handleChange}
        placeholder={placeholder}
        disabled={disabled}
        maxLength={maxLength}
        className={cn("pl-9", inputClassName)}
      />
    </div>
  );
}

export { TableSearchInput };
