import { useEffect, useRef, useState } from "react";
import { cn } from "@/util/utils";

interface Props {
  /**
   * The direction of the splitter. If "vertical", the split bar is vertical,
   * etc.
   */
  direction: "horizontal" | "vertical";
  children: React.ReactNode;
  /**
   * Optional `split` definition. If provided, only one child property should
   * be specified. If no child is specified, or `split` is not specified, then
   * the split sizing will be an even 50% for each side.
   */
  split?: {
    /**
     * A sizing in px, rem or %. "10px", "2rem", "30%, etc.
     */
    left?: string;
    /**
     * A sizing in px, rem or %. "10px", "2rem", "30%, etc.
     */
    right?: string;
    /**
     * A sizing in px, rem or %. "10px", "2rem", "30%, etc.
     */
    top?: string;
    /**
     * A sizing in px, rem or %. "10px", "2rem", "30%, etc.
     */
    bottom?: string;
  };
  /**
   * If you want to preserve the split sizing between reloads, provide a storage
   * key.
   */
  storageKey?: string;
}

type SizingTarget = "left" | "right" | "top" | "bottom";

const getStorageKey = (storageKey: string, firstSizingTarget: SizingTarget) => {
  return `skyvern.splitter.${storageKey}.${firstSizingTarget}`;
};

const getStoredSizing = (
  firstSizingTarget: SizingTarget | null,
  storageKey: string,
): string | null => {
  if (!firstSizingTarget) {
    return null;
  }

  const key = getStorageKey(storageKey, firstSizingTarget);
  const storedFirstSizing = localStorage.getItem(key);

  return storedFirstSizing;
};

const setStoredSizing = (
  sizingTarget: SizingTarget,
  storageKey: string,
  sizing: string,
) => {
  const key = getStorageKey(storageKey, sizingTarget);
  localStorage.setItem(key, sizing);
};

function Splitter({ children, direction, split, storageKey }: Props) {
  if (!Array.isArray(children) || children.length !== 2) {
    throw new Error("Splitter must have exactly two children");
  }

  const [firstChild, secondChild] = children;

  split = split || {};
  const splitterThickness = "5px";
  let firstSizing = "50%";
  let firstSizingTarget: SizingTarget | null = null;

  if (direction === "vertical") {
    if (split.left && split.right) {
      throw new Error(
        "Vertical splitter can only specify a split of left _or_ of right.",
      );
    }

    if (split.left) {
      firstSizing = split.left;
      firstSizingTarget = "left";
    } else if (split.right) {
      firstSizing = split.right;
      firstSizingTarget = "right";
    }
  } else if (direction === "horizontal") {
    if (split.top && split.bottom) {
      throw new Error(
        "Horizontal splitter can only specify a split of top _or_ of bottom.",
      );
    }

    if (split.top) {
      firstSizing = split.top;
      firstSizingTarget = "top";
    } else if (split.bottom) {
      firstSizing = split.bottom;
      firstSizingTarget = "bottom";
    }
  }

  if (storageKey) {
    const storedFirstSizing = getStoredSizing(firstSizingTarget, storageKey);

    if (storedFirstSizing) {
      firstSizing = storedFirstSizing;
    }
  }

  const containerRef = useRef<HTMLDivElement>(null);

  const onMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    const startCoord = direction === "vertical" ? e.clientX : e.clientY;
    const container = e.currentTarget.closest(".splitter") as HTMLDivElement;
    const containerSize =
      direction === "vertical"
        ? container?.offsetWidth
        : container?.offsetHeight;

    const initialPercent = splitPosition;
    const startWidth = (containerSize || 100) * (initialPercent / 100);

    const splitterThicknessPx = parseFloat(
      splitterThickness.replace(/[^0-9.]/g, ""),
    );
    const adjustedStartWidth = startWidth + splitterThicknessPx / 2;

    const onMouseMove = (moveEvent: MouseEvent) => {
      const delta =
        direction === "vertical"
          ? moveEvent.clientX - startCoord
          : moveEvent.clientY - startCoord;

      const newPixelPos = adjustedStartWidth + delta;
      const maxSize = containerSize || 100;
      const newWidth = (newPixelPos / maxSize) * 100;
      const clampedWidth = Math.max(0, Math.min(newWidth, 100));

      setSplitPosition(clampedWidth);
    };

    const onMouseUp = () => {
      setIsDragging(false);
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
    };

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  };

  const normalizeUnitsToPercent = (sizing: string): number => {
    const floatSizing = parseFloat(sizing);

    if (!isNaN(floatSizing)) {
      return floatSizing;
    }

    const defaultSizing = 50;
    const percentMatch = sizing.match(/([0-9.]+)%/);

    if (percentMatch) {
      return parseFloat(percentMatch[1] ?? defaultSizing.toString());
    }

    const container = containerRef.current;
    if (!container) {
      return defaultSizing;
    }

    const containerSize =
      direction === "vertical" ? container.offsetWidth : container.offsetHeight;

    // px
    const pxMatch = sizing.match(/([0-9.]+)px/);
    if (pxMatch) {
      const pxValue = parseFloat(pxMatch[1] ?? defaultSizing.toString());
      return (pxValue / containerSize) * 100;
    }

    // rem
    const remMatch = sizing.match(/([0-9.]+)rem/);
    if (remMatch) {
      const remValue = parseFloat(remMatch[1] ?? defaultSizing.toString());
      const pxValue = remValue * 16; // Assuming 1rem = 16px
      return (pxValue / containerSize) * 100;
    }

    const fallbackMatch = sizing.match(/([0-9.]+)/);
    if (fallbackMatch) {
      const numValue = parseFloat(fallbackMatch[1] ?? defaultSizing.toString());
      return numValue;
    }

    return defaultSizing;
  };

  const [splitPosition, setSplitPosition] = useState<number>(
    normalizeUnitsToPercent(firstSizing),
  );

  const [isDragging, setIsDragging] = useState(false);

  useEffect(() => {
    if (containerRef.current) {
      const newPosition = normalizeUnitsToPercent(firstSizing);
      setSplitPosition(newPosition);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [containerRef.current]);

  useEffect(() => {
    if (storageKey && firstSizingTarget) {
      setStoredSizing(firstSizingTarget, storageKey, splitPosition.toString());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [splitPosition]);

  return (
    <div
      className={cn(
        "splitter flex h-full w-full",
        direction === "vertical" ? "flex-row" : "flex-col",
      )}
      ref={containerRef}
    >
      {direction === "vertical" ? (
        <>
          <div
            className={cn("left h-full", {
              "pointer-events-none cursor-col-resize select-none": isDragging,
            })}
            style={{
              width: `calc(${splitPosition}% - (${splitterThickness} / 2))`,
            }}
          >
            {firstChild}
          </div>
          <div
            className={cn(
              "splitter-bar z-[100] h-full w-[5px] cursor-col-resize bg-[#ccc] opacity-10 hover:opacity-90",
              { "opacity-90": isDragging },
            )}
            onMouseDown={onMouseDown}
          ></div>
          <div
            className={cn("right h-full", {
              "pointer-events-none cursor-col-resize select-none": isDragging,
            })}
            style={{
              width: `calc(100% - ${splitPosition}% - (${splitterThickness} / 2))`,
            }}
          >
            {secondChild}
          </div>
        </>
      ) : (
        <>
          <div
            className={cn("top h-full", {
              "pointer-events-none cursor-row-resize select-none": isDragging,
            })}
            style={{
              height: `calc(${splitPosition}% - (${splitterThickness} / 2))`,
            }}
          >
            {firstChild}
          </div>
          <div
            className={cn(
              "splitter-bar z-[100] h-[5px] w-full cursor-row-resize bg-[#ccc] opacity-10 hover:opacity-90",
              { "opacity-90": isDragging },
            )}
            onMouseDown={onMouseDown}
          ></div>
          <div
            className={cn("bottom h-full", {
              "pointer-events-none cursor-row-resize select-none": isDragging,
            })}
            style={{
              height: `calc(100% - ${splitPosition}% - (${splitterThickness} / 2))`,
            }}
          >
            {secondChild}
          </div>
        </>
      )}
    </div>
  );
}

export { Splitter };
