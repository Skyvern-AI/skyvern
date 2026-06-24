import { CheckIcon } from "@radix-ui/react-icons";
import { cn } from "@/util/utils";
import {
  TAG_COLOR_PALETTE,
  paletteSwatchClass,
  type PaletteColorName,
} from "../../types/tagColors";

type Props = {
  value: PaletteColorName;
  onChange: (color: PaletteColorName) => void;
  className?: string;
};

// A row of palette swatches for choosing a grouped tag's color. The selected
// swatch shows a ring and a check.
function TagColorSwatchPicker({ value, onChange, className }: Props) {
  return (
    <div className={cn("flex flex-wrap gap-1.5", className)}>
      {TAG_COLOR_PALETTE.map((color) => {
        const selected = color === value;
        return (
          <button
            key={color}
            type="button"
            aria-label={color}
            aria-pressed={selected}
            title={color}
            onClick={() => onChange(color)}
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded-full ring-offset-background transition-shadow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2",
              paletteSwatchClass(color),
              selected
                ? "ring-2 ring-ring ring-offset-2"
                : "hover:ring-2 hover:ring-ring/40 hover:ring-offset-1",
            )}
          >
            {selected ? (
              // Dark halo keeps the white check legible on the lightest swatches
              // (yellow/amber) as well as the dark ones.
              <CheckIcon className="h-3.5 w-3.5 text-white [filter:drop-shadow(0_0_1.5px_rgba(0,0,0,0.65))]" />
            ) : null}
          </button>
        );
      })}
    </div>
  );
}

export { TagColorSwatchPicker };
