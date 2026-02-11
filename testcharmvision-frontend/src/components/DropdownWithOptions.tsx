import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "./ui/select";

type Item = {
  label: string;
  value: string;
};

type Props = {
  options: Item[];
  value: string;
  onChange: (selected: string) => void;
  placeholder?: string;
  className?: string;
};

function DropdownWithOptions({
  options,
  value,
  onChange,
  placeholder,
  className,
}: Props) {
  return (
    <Select
      value={value}
      onValueChange={(value) => {
        onChange(value);
      }}
    >
      <SelectTrigger className={className}>
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent className="max-h-48">
        {options.map((option) => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export { DropdownWithOptions };
