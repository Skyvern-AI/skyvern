import { useRef, useState, RefObject } from "react";
import { cn } from "@/util/utils";
import { useOnChange } from "@/hooks/useOnChange";
import { useMountEffect } from "@/hooks/useMountEffect";

function Handle({
  direction,
  isDragging,
  onDoubleClick,
}: {
  direction: "vertical" | "horizontal";
  isDragging: boolean;
  onDoubleClick?: () => void;
}) {
  return (
    <div
      className={cn(
        "absolute flex h-[1.25rem] w-[10px] flex-wrap items-center justify-center gap-[2px] bg-slate-800 pb-1 pt-1",
        {
          "cursor-col-resize": direction === "vertical",
          "cursor-row-resize": direction === "horizontal",
          "bg-slate-700": isDragging,
        },
      )}
      onDoubleClick={() => onDoubleClick?.()}
    >
      <div className="flex w-full items-center justify-center gap-[0.15rem]">
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
      </div>
      <div className="flex w-full items-center justify-center gap-[0.15rem]">
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
      </div>
      <div className="flex w-full items-center justify-center gap-[0.15rem]">
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
        <div
          className={cn("h-[2px] w-[2px] rounded-full bg-[#666]", {
            "bg-[#222]": isDragging,
          })}
        />
      </div>
    </div>
  );
}

interface Props {
  className?: string;
  classNameLeft?: string;
  classNameRight?: string;
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
  /**
   * Callback fired when the splitter is resized
   */
  onResize?: () => void;
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

const getFirstSizing = (
  direction: Props["direction"],
  split: Props["split"],
  storageKey: Props["storageKey"],
): [string, SizingTarget] => {
  split = split || {};
  let firstSizing = "50%";
  let firstSizingTarget: SizingTarget = "left";

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
  } else {
    throw new Error(`Invalid direction: ${direction}`);
  }

  if (storageKey) {
    const storedFirstSizing = getStoredSizing(firstSizingTarget, storageKey);

    if (storedFirstSizing) {
      firstSizing = storedFirstSizing;
    }
  }

  return [firstSizing, firstSizingTarget];
};

const normalize = (sizing: string, containerSize: number) => {
  const defaultSizing = 50;
  const percentMatch = sizing.match(/([0-9.]+)%/);

  if (percentMatch) {
    return parseFloat(percentMatch[1] ?? defaultSizing.toString());
  }

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

const normalizeUnitsToPercent = (
  containerRef: RefObject<HTMLDivElement | null>,
  direction: Props["direction"],
  firstSizingTarget: SizingTarget,
  sizing: string,
  storageKey?: string,
): number => {
  const lastChar = parseFloat(sizing.charAt(sizing.length - 1));

  if (!isNaN(lastChar)) {
    const floatSizing = parseFloat(sizing);

    if (!isNaN(floatSizing)) {
      return floatSizing;
    }
  }

  const container = containerRef.current;

  if (!container) {
    return 50;
  }

  const containerSize =
    direction === "vertical" ? container.offsetWidth : container.offsetHeight;

  if (storageKey) {
    const stored = getStoredSizing(firstSizingTarget, storageKey);

    if (stored) {
      return parseFloat(stored);
    }
  }

  const normalized = normalize(sizing, containerSize);

  if (firstSizingTarget === "right" || firstSizingTarget === "bottom") {
    return 100 - normalized;
  }

  return normalized;
};

const setStoredSizing = (
  firstSizingTarget: SizingTarget,
  storageKey: string,
  sizing: string,
) => {
  const key = getStorageKey(storageKey, firstSizingTarget);
  localStorage.setItem(key, sizing);
};

function Splitter({
  children,
  className,
  classNameLeft,
  classNameRight,
  direction,
  split,
  storageKey,
  onResize,
}: Props) {
  if (!Array.isArray(children) || children.length !== 2) {
    throw new Error("Splitter must have exactly two children");
  }

  const [firstChild, secondChild] = children;
  const containerRef = useRef<HTMLDivElement>(null);
  const splitterThickness = "5px";

  const [firstSizing, firstSizingTarget] = getFirstSizing(
    direction,
    split,
    storageKey,
  );

  const onMouseDown = (e: React.MouseEvent) => {
    setIsDragging(true);
    document.body.classList.add("no-select-global");
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

      setIsClosed(false);
      setSplitPosition(clampedWidth);
      onResize?.();
    };

    const onMouseUp = () => {
      setIsDragging(false);
      document.body.classList.remove("no-select-global");
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      onResize?.();
    };

    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
  };

  const [splitPosition, setSplitPosition] = useState<number>(50);
  const [isClosed, setIsClosed] = useState(false);
  const [closedSplitPosition, setClosedSplitPosition] =
    useState<number>(splitPosition);
  const [isDragging, setIsDragging] = useState(false);

  useMountEffect(() => {
    if (containerRef.current) {
      const newPosition = normalizeUnitsToPercent(
        containerRef,
        direction,
        firstSizingTarget,
        firstSizing,
        storageKey,
      );

      setSplitPosition(newPosition);

      if (storageKey) {
        setStoredSizing(firstSizingTarget, storageKey, newPosition.toString());
      }
    }
  });

  useOnChange(isDragging, (newValue, oldValue) => {
    if (!newValue && oldValue) {
      if (storageKey) {
        setStoredSizing(
          firstSizingTarget,
          storageKey,
          splitPosition.toString(),
        );
      }
    }
  });

  /**
   * A "snap" is:
   * - if the splitter is not "closed", then "close it"
   * - if the splitter is "closed", then "open it"
   *
   * "closed" depends on the `split` prop definition. For instance, if the
   * `split` prop has `left` defined, then "closing" is the `left` side
   * resizing down to 0.
   *
   * When "closing", the current splitPosition should be memorized and then
   * returned to when an "open" happens.
   */
  const snap = () => {
    if (isClosed) {
      setSplitPosition(closedSplitPosition);
    } else {
      setClosedSplitPosition(splitPosition);
      setSplitPosition(0);
    }
    setIsClosed(!isClosed);
    onResize?.();
  };

  return (
    <div
      className={cn(
        "splitter flex h-full w-full overflow-hidden",
        direction === "vertical" ? "flex-row" : "flex-col",
        className || "",
      )}
      ref={containerRef}
    >
      {direction === "vertical" ? (
        <>
          <div
            className={cn(
              "left h-full",
              {
                "pointer-events-none cursor-col-resize select-none opacity-80":
                  isDragging,
                "overflow-x-hidden": direction === "vertical",
                "overflow-y-hidden": direction !== "vertical",
              },
              classNameLeft,
            )}
            style={{
              width: `calc(${splitPosition}% - (${splitterThickness} / 2))`,
            }}
          >
            {firstChild}
          </div>
          <div
            className={cn(
              "splitter-bar relative z-[0] flex h-full w-[10px] cursor-col-resize items-center justify-center opacity-50 hover:opacity-100",
              { "opacity-90": isDragging },
            )}
            onMouseDown={onMouseDown}
            onDoubleClick={snap}
          >
            <div
              className={cn("h-full w-[2px] bg-slate-800", {
                "bg-slate-700": isDragging,
              })}
            />
            <Handle
              direction={direction}
              isDragging={isDragging}
              onDoubleClick={snap}
            />
          </div>
          <div
            className={cn(
              "right h-full",
              {
                "pointer-events-none cursor-col-resize select-none opacity-80":
                  isDragging,
                "overflow-x-hidden": direction === "vertical",
                "overflow-y-hidden": direction !== "vertical",
              },
              classNameRight,
            )}
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
              "pointer-events-none cursor-row-resize select-none opacity-80":
                isDragging,
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
              "pointer-events-none cursor-row-resize select-none opacity-80":
                isDragging,
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
