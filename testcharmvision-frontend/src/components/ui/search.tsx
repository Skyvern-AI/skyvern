import * as React from "react";
import { Input } from "./input";
import { SearchIcon } from "../icons/SearchIcon";
import { cn } from "@/util/utils";

interface SearchProps extends React.InputHTMLAttributes<HTMLInputElement> {
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  className?: string;
  label?: string;
}

export const Search = React.forwardRef<HTMLInputElement, SearchProps>(
  (
    {
      value,
      onChange,
      placeholder = "Search...",
      className,
      label = "Search",
      ...props
    },
    ref,
  ) => {
    return (
      <div className={cn("min-w-6rem relative flex items-center", className)}>
        <label htmlFor={props.id} className="sr-only">
          {label}
        </label>
        <span className="pointer-events-none absolute left-3 text-muted-foreground">
          <SearchIcon className="h-4 w-4" />
        </span>
        <Input
          ref={ref}
          value={value}
          onChange={onChange}
          placeholder={placeholder}
          className={cn(
            "h-full rounded-sm py-3.5 pl-10",
            props.disabled && "opacity-50",
          )}
          aria-label={label}
          {...props}
        />
      </div>
    );
  },
);
Search.displayName = "Search";

export default Search;
