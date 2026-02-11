import { ReactNode, Children, useRef, useEffect } from "react";

import { useRecordingStore } from "@/store/useRecordingStore";
import { cn } from "@/util/utils";

interface FlippableProps {
  facing?: "front" | "back";
  children: ReactNode;
  className?: string;
  /**
   * If `true`, then the height of the content of the front, whatever it happens
   * to be, is marked right before the flip-from-front-to-back takes place.
   * This height is then applied to the content in the back. This synchronizes
   * the front content height onto the back content height.
   *
   * Default is `false`.
   */
  preserveFrontsideHeight?: boolean;
}

export function Flippable({
  facing = "front",
  children,
  className,
  preserveFrontsideHeight = false,
}: FlippableProps) {
  const recordingStore = useRecordingStore();
  const childrenArray = Children.toArray(children);
  const front = childrenArray[0];
  const back = childrenArray[1];

  const frontRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLDivElement>(null);
  const capturedHeightRef = useRef<number | null>(null);

  useEffect(() => {
    if (
      preserveFrontsideHeight &&
      facing === "back" &&
      frontRef.current &&
      backRef.current
    ) {
      if (capturedHeightRef.current === null) {
        capturedHeightRef.current = frontRef.current.offsetHeight;
      }
      backRef.current.style.height = `${capturedHeightRef.current}px`;
    } else if (facing === "front" && backRef.current) {
      backRef.current.style.height = "auto";
      capturedHeightRef.current = null;
    }
  }, [facing, preserveFrontsideHeight]);

  return (
    <div
      className={cn(className, {
        "pointer-events-none opacity-50": recordingStore.isRecording,
      })}
      style={{ perspective: "1000px" }}
    >
      <div
        className={cn(
          "transition-transform duration-700",
          "transform-style-preserve-3d",
          {
            "rotate-y-180": facing === "back",
          },
        )}
        style={{
          transformStyle: "preserve-3d",
          transform: facing === "back" ? "rotateY(180deg)" : "rotateY(0deg)",
          transition: "transform 0.7s cubic-bezier(0.68, -0.55, 0.265, 1.55)",
        }}
      >
        <div
          ref={frontRef}
          style={{
            backfaceVisibility: "hidden",
            WebkitBackfaceVisibility: "hidden",
          }}
        >
          {front}
        </div>
        <div
          ref={backRef}
          className="absolute inset-0 flex items-start justify-start"
          style={{
            backfaceVisibility: "hidden",
            WebkitBackfaceVisibility: "hidden",
            transform: "rotateY(180deg)",
          }}
        >
          {back}
        </div>
      </div>
    </div>
  );
}
