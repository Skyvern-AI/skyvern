import { Checkbox } from "@/components/ui/checkbox";
import { TableCell, TableHead } from "@/components/ui/table";
import { cn } from "@/util/utils";

type SelectionCheckboxProps = {
  index: number; // -1 = inert (non-selectable row)
  checked: boolean;
  hasSelection: boolean; // any selection active keeps boxes visible
  onSelect: (index: number, shiftKey: boolean) => void;
  ariaLabel: string;
};

function SelectionCheckbox({
  index,
  checked,
  hasSelection,
  onSelect,
  ariaLabel,
}: SelectionCheckboxProps) {
  return (
    <div
      className="select-none"
      onMouseDown={(event) => {
        if (event.shiftKey) {
          event.preventDefault();
        }
      }}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        if (index >= 0) {
          onSelect(index, event.shiftKey);
        }
      }}
    >
      <Checkbox
        checked={checked}
        aria-label={ariaLabel}
        // Mouse goes through the container for shift-range; onCheckedChange covers keyboard.
        onCheckedChange={() => {
          if (index >= 0) {
            onSelect(index, false);
          }
        }}
        className={cn(
          "pointer-events-none transition-opacity",
          !hasSelection &&
            "opacity-0 focus-visible:opacity-100 group-hover/row:opacity-100 group-data-[row-active]/row:opacity-100",
        )}
      />
    </div>
  );
}

function SelectionCheckboxCell(
  props: SelectionCheckboxProps & { className?: string },
) {
  const { className, ...checkbox } = props;
  return (
    <TableCell className={className}>
      <SelectionCheckbox {...checkbox} />
    </TableCell>
  );
}

function SelectionHeaderCheckboxCell({
  allSelected,
  someSelected,
  hasSelection,
  onToggleAll,
  ariaLabel,
  className,
}: {
  allSelected: boolean;
  someSelected: boolean;
  hasSelection: boolean;
  onToggleAll: () => void;
  ariaLabel: string;
  className?: string;
}) {
  return (
    <TableHead className={className}>
      <Checkbox
        checked={someSelected ? "indeterminate" : allSelected}
        onCheckedChange={onToggleAll}
        aria-label={ariaLabel}
        className={cn(
          "transition-opacity",
          !hasSelection &&
            "opacity-0 focus-visible:opacity-100 group-hover/header:opacity-100",
        )}
      />
    </TableHead>
  );
}

export {
  SelectionCheckbox,
  SelectionCheckboxCell,
  SelectionHeaderCheckboxCell,
};
