import { ReactNode, useEffect, useRef, useState } from "react";

import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";

import "./WorkflowAdderBusy.css";

type Operation = "recording" | "processing";

type Size = "small" | "large";

type Props = {
  children: ReactNode;
  /**
   * The operation being performed (e.g., recording or processing).
   */
  operation: Operation;
  /**
   * An explicit sizing; otherwise the size will be determined by the child content.
   */
  size?: Size;
  /**
   * Color for the cover and ellipses. Defaults to "red".
   */
  color?: string;
  // --
  onComplete: () => void;
};

function WorkflowAdderBusy({
  children,
  operation,
  size,
  color = "red",
  onComplete,
}: Props) {
  const recordingStore = useRecordingStore();
  const [isHovered, setIsHovered] = useState(false);
  const [shouldBump, setShouldBump] = useState(false);
  const bumpTimeoutRef = useRef<NodeJS.Timeout | null>(null);
  const prevCountRef = useRef(0);
  const eventCount = recordingStore.exposedEventCount;

  // effect for bump animation when count changes
  useEffect(() => {
    if (eventCount > prevCountRef.current && prevCountRef.current > 0) {
      if (bumpTimeoutRef.current) {
        clearTimeout(bumpTimeoutRef.current);
      }

      setShouldBump(true);

      bumpTimeoutRef.current = setTimeout(() => {
        setShouldBump(false);
      }, 300);
    }

    prevCountRef.current = eventCount;

    return () => {
      if (bumpTimeoutRef.current) {
        clearTimeout(bumpTimeoutRef.current);
      }
    };
  }, [eventCount]);

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    onComplete();

    return false;
  };

  return (
    <TooltipProvider>
      <div className="relative inline-block">
        <Tooltip open={isHovered}>
          <TooltipTrigger asChild>
            <div
              className={cn("relative inline-block", {
                "flex items-center justify-center": size !== undefined,
                "min-h-[40px] min-w-[40px]": size === "small",
                "min-h-[80px] min-w-[80px]": size === "large",
              })}
              onMouseEnter={() => setIsHovered(true)}
              onMouseLeave={() => setIsHovered(false)}
            >
              {/* cover */}
              <div
                className={cn("absolute inset-0 rounded-full opacity-40", {
                  "opacity-30": isHovered,
                })}
                style={{ backgroundColor: color }}
                onClick={handleClick}
              />
              <div className="pointer-events-none flex items-center justify-center">
                {children}
              </div>
              <div className="pointer-events-none absolute inset-0">
                <svg
                  className="h-full w-full animate-spin"
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                  style={{ transformOrigin: "center" }}
                >
                  <ellipse
                    cx="50"
                    cy="50"
                    rx="45"
                    ry="45"
                    fill="none"
                    stroke={color}
                    strokeWidth={size === "small" ? "3" : "6"}
                    strokeDasharray="141.4 141.4"
                    strokeLinecap="round"
                    vectorEffect="non-scaling-stroke"
                    style={{
                      animation: `${size === "small" ? "pulse-dash-small" : "pulse-dash"} 10s ease-in-out infinite`,
                    }}
                  />
                </svg>
              </div>
              {isHovered && (
                <div className="pointer-events-none absolute inset-0">
                  <svg
                    className="h-full w-full"
                    viewBox="0 0 100 100"
                    preserveAspectRatio="none"
                  >
                    <rect
                      x="30"
                      y="30"
                      width="40"
                      height="40"
                      fill={color}
                      vectorEffect="non-scaling-stroke"
                      className="animate-in zoom-in-0"
                      style={{
                        transformOrigin: "center",
                        transformBox: "fill-box",
                        animationDuration: "200ms",
                        animationTimingFunction:
                          "cubic-bezier(0.34, 1.56, 0.64, 1)",
                      }}
                    />
                  </svg>
                </div>
              )}
            </div>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              {operation === "recording" ? "Finish Recording" : "Processing..."}
            </p>
          </TooltipContent>
        </Tooltip>
        {recordingStore.isRecording && eventCount > 0 && (
          <Tooltip delayDuration={0}>
            <TooltipTrigger asChild>
              <div
                className={cn(
                  "absolute -right-2 -top-2 flex h-6 min-w-6 items-center justify-center rounded-full px-1.5 text-xs font-semibold text-white shadow-lg transition-transform",
                  {
                    "scale-125": shouldBump,
                    "scale-100": !shouldBump,
                  },
                )}
                style={{
                  backgroundColor: color,
                  transition: "transform 0.6s",
                }}
              >
                {eventCount}
              </div>
            </TooltipTrigger>
            <TooltipContent>
              <p>Event Count</p>
            </TooltipContent>
          </Tooltip>
        )}
      </div>
    </TooltipProvider>
  );
}

export { WorkflowAdderBusy };
