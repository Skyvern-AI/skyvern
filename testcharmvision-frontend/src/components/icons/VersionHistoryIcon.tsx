import { CounterClockwiseClockIcon, FileTextIcon } from "@radix-ui/react-icons";

type Props = {
  /** Size of the main document icon in pixels */
  size?: number;
  /** Additional class names */
  className?: string;
};

function VersionHistoryIcon({ size = 24, className = "" }: Props) {
  // Calculate relative size for the history icon (overlay)
  const historySize = Math.round(size * 0.6);

  return (
    <div
      className={`relative inline-flex items-center justify-center ${className}`}
      style={{ width: size, height: size }}
      aria-label="Version History"
    >
      {/* Main Document Icon */}
      <FileTextIcon width={size} height={size} />

      {/* Overlay History Icon - Bottom Left */}
      <div
        className="absolute bottom-0 left-0 rounded-full bg-slate-elevation2"
        style={{
          transform: "translate(-20%, 20%)",
        }}
      >
        <CounterClockwiseClockIcon width={historySize} height={historySize} />
      </div>
    </div>
  );
}

export { VersionHistoryIcon };
