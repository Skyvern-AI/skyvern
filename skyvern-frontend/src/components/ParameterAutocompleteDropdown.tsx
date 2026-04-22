import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import type {
  AvailableParameter,
  ParameterCategory,
} from "@/hooks/useAvailableParameters";

type Props = {
  items: AvailableParameter[];
  selectedIndex: number;
  anchorPosition: { top: number; left: number };
  visible: boolean;
  onSelect: (key: string) => void;
  onDismiss: () => void;
};

const CATEGORY_LABELS: Record<ParameterCategory, string> = {
  parameter: "Parameters",
  output: "Block Outputs",
  system: "System",
};

const CATEGORY_ORDER: ParameterCategory[] = ["parameter", "output", "system"];

function groupByCategory(
  items: AvailableParameter[],
): Map<ParameterCategory, AvailableParameter[]> {
  const groups = new Map<ParameterCategory, AvailableParameter[]>();
  for (const item of items) {
    const group = groups.get(item.category) ?? [];
    group.push(item);
    groups.set(item.category, group);
  }
  return groups;
}

function ParameterAutocompleteDropdown({
  items,
  selectedIndex,
  anchorPosition,
  visible,
  onSelect,
  onDismiss,
}: Props) {
  const listRef = useRef<HTMLDivElement>(null);
  const selectedRef = useRef<HTMLDivElement>(null);

  // Scroll the selected item into view
  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [selectedIndex]);

  // Dismiss on outside click or scroll (prevents position drift in React Flow canvas)
  useEffect(() => {
    if (!visible) return;

    const handleMouseDown = (e: MouseEvent) => {
      if (listRef.current && !listRef.current.contains(e.target as Node)) {
        onDismiss();
      }
    };
    const handleScroll = (e: Event) => {
      // Ignore scroll events from within the dropdown itself (e.g. scrollIntoView)
      if (listRef.current && listRef.current.contains(e.target as Node)) {
        return;
      }
      onDismiss();
    };
    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("scroll", handleScroll, true);
    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("scroll", handleScroll, true);
    };
  }, [visible, onDismiss]);

  if (!visible || items.length === 0) return null;

  const grouped = groupByCategory(items);
  let flatIndex = 0;

  const content = (
    <div
      ref={listRef}
      className="fixed z-[9999] max-h-64 w-64 overflow-y-auto rounded-md border border-border bg-slate-elevation1 py-1 shadow-lg"
      style={{
        top: anchorPosition.top,
        left: anchorPosition.left,
      }}
    >
      {CATEGORY_ORDER.map((category) => {
        const group = grouped.get(category);
        if (!group || group.length === 0) return null;
        return (
          <div key={category}>
            <div className="px-2 py-1 text-[0.625rem] font-medium uppercase tracking-wide text-muted-foreground">
              {CATEGORY_LABELS[category]}
            </div>
            {group.map((param) => {
              const currentIndex = flatIndex++;
              const isSelected = currentIndex === selectedIndex;
              return (
                <div
                  key={`${category}-${param.key}`}
                  ref={isSelected ? selectedRef : undefined}
                  className={`cursor-pointer px-2 py-1.5 text-xs ${
                    isSelected
                      ? "bg-slate-elevation2 text-foreground"
                      : "text-muted-foreground hover:bg-slate-elevation2"
                  }`}
                  onMouseDown={(e) => {
                    e.preventDefault(); // Prevent blur on the input
                    onSelect(param.key);
                  }}
                >
                  <div className="font-mono">{param.key}</div>
                  {param.description && (
                    <div className="text-[0.625rem] text-muted-foreground">
                      {param.description}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );

  return createPortal(content, document.body);
}

export { ParameterAutocompleteDropdown };
