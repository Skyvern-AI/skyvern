import { useState, useRef, useEffect, ReactNode, Fragment } from "react";

export interface RadialMenuItem {
  id: string;
  enabled?: boolean;
  hidden?: boolean;
  text?: string;
  icon: React.ReactNode;
  onClick: () => void;
}

interface RadialMenuProps {
  items: RadialMenuItem[];
  children: ReactNode;
  /**
   * The size of the buttons in CSS pixel units (e.g., "40px"). If not provided,
   * defaults to "40px".
   */
  buttonSize?: string;
  /**
   * The gap between items in degrees. If not provided, items are evenly spaced
   * around the circle.
   */
  gap?: number;
  /**
   * The radius of the radial menu, in CSS pixel units (e.g., "100px").
   */
  radius?: string;
  /**
   * The starting angle offset in degrees for the first item. If not provided,
   * defaults to 0 degrees (top of the circle).
   */
  startAt?: number;
  /**
   * If true, rotates the text so its baseline runs parallel to the radial line.
   */
  rotateText?: boolean;
}

const proportionalAngle = (
  index: number,
  numItems: number,
  startAtDegrees: number = 0,
) => {
  const normalizedStart = ((startAtDegrees % 360) + 360) % 360;
  const startRadians = (normalizedStart * Math.PI) / 180;

  const angleStep = (2 * Math.PI) / numItems;
  const angle = angleStep * index - Math.PI / 2 + startRadians;

  return angle;
};

const gappedAngle = (
  index: number,
  gapDegrees: number,
  startAtDegrees: number = 0,
) => {
  const normalizedGap = ((gapDegrees % 360) + 360) % 360;
  const normalizedStart = ((startAtDegrees % 360) + 360) % 360;
  const gapRadians = (normalizedGap * Math.PI) / 180;
  const startRadians = (normalizedStart * Math.PI) / 180;

  // each item is the previous item's angle + gap tart from top (-PI/2) + startAt offset,
  // then add gap * index
  const angle = -Math.PI / 2 + startRadians + index * gapRadians;

  return angle;
};

export function RadialMenu({
  items,
  children,
  buttonSize,
  radius,
  gap,
  startAt,
  rotateText,
}: RadialMenuProps) {
  const [isOpen, setIsOpen] = useState(false);
  const [calculatedRadius, setCalculatedRadius] = useState<number>(100);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const dom = {
    root: useRef<HTMLDivElement>(null),
  };

  // effect to calculate radius based on wrapped component size if not provided
  useEffect(() => {
    if (!radius && wrapperRef.current) {
      const { width, height } = wrapperRef.current.getBoundingClientRect();
      const minDimension = Math.min(width, height);
      setCalculatedRadius(minDimension);
    } else if (radius) {
      const numericRadius = parseFloat(radius);
      setCalculatedRadius(numericRadius);
    }
  }, [radius, children]);

  const radiusValue = radius || `${calculatedRadius}px`;
  const numRadius = parseFloat(radiusValue);
  const visibleItems = items.filter((item) => !item.hidden);
  const padSizeVertical = numRadius * 2;
  const padSizeHorizontal = padSizeVertical * 4;

  return (
    <div
      ref={dom.root}
      className="relative z-[1000000]"
      onMouseLeave={() => {
        setIsOpen(false);
      }}
    >
      {/* a pad (buffer) to increase the deactivation area when leaving the component */}
      <div
        className="absolute left-1/2 top-1/2 z-10"
        style={{
          width: `${padSizeHorizontal}px`,
          height: `${padSizeVertical}px`,
          pointerEvents: isOpen ? "auto" : "none",
          transform: "translate(-50%, -50%)",
        }}
      />

      <div
        ref={wrapperRef}
        className="relative z-20"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          setIsOpen(!isOpen);
        }}
        onMouseEnter={() => {
          setIsOpen(true);
        }}
      >
        <div className="pointer-events-none">{children}</div>
      </div>

      {visibleItems.map((item, index) => {
        const angle =
          gap !== undefined
            ? gappedAngle(index, gap, startAt)
            : proportionalAngle(index, visibleItems.length, startAt);
        const x = Math.cos(angle) * parseFloat(radiusValue);
        const y = Math.sin(angle) * parseFloat(radiusValue);
        const isEnabled = item.enabled !== false;

        // calculate text offset along the radial line
        const textDistance = 0.375 * numRadius;
        const textX = Math.cos(angle) * textDistance;
        const textY = Math.sin(angle) * textDistance;

        // convert angle from radians to degrees for CSS rotation
        const angleDegrees = (angle * 180) / Math.PI;
        // normalize angle to 0-360 range
        const normalizedAngle = ((angleDegrees % 360) + 360) % 360;
        // flip text if it would be upside-down (between 90° and 270°)
        const textTransform =
          normalizedAngle > 90 && normalizedAngle < 270
            ? "scaleY(-1) scaleX(-1)"
            : "scaleY(1)";

        return (
          <Fragment key={item.id}>
            <button
              onClick={() => {
                item.onClick();
                setIsOpen(false);
              }}
              disabled={!isEnabled}
              className="absolute left-1/2 top-1/2 z-30 flex items-center justify-center rounded-full bg-white shadow-lg transition-all duration-300 ease-out hover:bg-gray-50 disabled:cursor-not-allowed"
              style={{
                width: buttonSize ?? "40px",
                height: buttonSize ?? "40px",
                transform: isOpen
                  ? `translate(-50%, -50%) translate(${x}px, ${y}px) scale(1)`
                  : "translate(-50%, -50%) translate(0, 0) scale(0)",
                opacity: isOpen ? (isEnabled ? 1 : 0.5) : 0,
                pointerEvents: isOpen ? "auto" : "none",
                transitionDelay: isOpen ? `${index * 50}ms` : "0ms",
              }}
            >
              <div className="text-gray-700">{item.icon}</div>
            </button>
            {item.text && (
              <div
                key={`${item.id}-text`}
                onClick={() => {
                  item.onClick();
                  setIsOpen(false);
                }}
                className="absolute left-1/2 top-1/2 z-30 cursor-pointer whitespace-nowrap rounded bg-white px-2 py-1 text-xs text-gray-700 shadow-md transition-all duration-300 ease-out"
                style={{
                  transform: isOpen
                    ? rotateText
                      ? `translate(0%, -50%) translate(${x + textX}px, ${y + textY}px) rotate(${angleDegrees}deg) scale(1)`
                      : `translate(0%, -50%) translate(${x + textX}px, ${y + textY}px) scale(1)`
                    : `translate(0%, -50%) translate(0, 0) scale(0.5) rotate(${angleDegrees}deg)`,
                  opacity: isOpen ? (isEnabled ? 1 : 0.5) : 0,
                  transformOrigin: "left center",
                  pointerEvents: isOpen ? "auto" : "none",
                  transitionDelay: isOpen ? `${index * 50}ms` : "0ms",
                }}
              >
                <span
                  style={{
                    display: "inline-block",
                    transform: textTransform,
                    opacity: isEnabled ? 1 : 0.5,
                    cursor: isEnabled ? "pointer" : "not-allowed",
                  }}
                >
                  {item.text}
                </span>
              </div>
            )}
          </Fragment>
        );
      })}
    </div>
  );
}
