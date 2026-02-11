import React, { useMemo, useId } from "react";

type Breakpoints = Record<number, number>; // { columns: minWidth }

type GridFormProps = {
  breakpoints: Breakpoints;
  className?: string;
  children: React.ReactNode;
};

/**
 * GridForm is a layout component that wraps its children in a CSS grid.
 * Pass the breakpoints prop as an object mapping columns to min viewport width.
 *
 * Example usage:
 * <GridForm breakpoints={{ 1: 600, 2: 900 }}>
 *   <Item1 />
 *   <Item2 />
 * </GridForm>
 */
export const GridForm: React.FC<GridFormProps> = ({
  breakpoints,
  className = "",
  children,
}) => {
  // Generate a unique className for this instance
  const uniqueClass = `grid-form-${useId().replace(/:/g, "-")}`;

  // Generate CSS for breakpoints
  const styleTag = useMemo(() => {
    // Sort breakpoints by minWidth ascending
    const sorted = Object.entries(breakpoints)
      .map(
        ([cols, minWidth]) =>
          [parseInt(cols, 10), minWidth] as [number, number],
      )
      .sort((a, b) => a[1] - b[1]);
    let css = `.${uniqueClass} { display: grid; gap: 1rem; }\n`;
    for (const [cols, minWidth] of sorted) {
      css += `@media (min-width: ${minWidth}px) { .${uniqueClass} { grid-template-columns: repeat(${cols}, minmax(0, 1fr)); } }\n`;
    }
    // Default to the smallest breakpoint (first one)
    if (sorted.length > 0 && sorted[0]) {
      const [firstCols, firstMinWidth] = sorted[0];
      css += `@media (max-width: ${firstMinWidth}px) { .${uniqueClass} { grid-template-columns: repeat(${firstCols}, minmax(0, 1fr)); } }\n`;
    }
    return <style>{css}</style>;
  }, [breakpoints, uniqueClass]);

  return (
    <>
      {styleTag}
      <div className={`${uniqueClass} ${className}`}>{children}</div>
    </>
  );
};

GridForm.displayName = "GridForm";

export default GridForm;
