import type { CSSProperties, RefObject } from "react";

type Props = {
  ghostText: string;
  textBeforeCursor: string;
  inputRef: RefObject<HTMLTextAreaElement | HTMLInputElement | null>;
  variant: "input" | "textarea";
};

function ParameterGhostText({
  ghostText,
  textBeforeCursor,
  inputRef,
  variant,
}: Props) {
  if (!ghostText || !inputRef.current) return null;

  const el = inputRef.current;
  const computed = window.getComputedStyle(el);

  const baseStyle: CSSProperties = {
    position: "absolute",
    top: 0,
    left: 0,
    right: 0,
    bottom: 0,
    pointerEvents: "none",
    overflow: "hidden",
    fontFamily: computed.fontFamily,
    fontSize: computed.fontSize,
    fontWeight: computed.fontWeight,
    lineHeight: computed.lineHeight,
    letterSpacing: computed.letterSpacing,
    paddingTop: computed.paddingTop,
    paddingRight: computed.paddingRight,
    paddingBottom: computed.paddingBottom,
    paddingLeft: computed.paddingLeft,
    borderWidth: computed.borderWidth,
    borderColor: "transparent",
    borderStyle: computed.borderStyle,
  };

  if (variant === "input") {
    return (
      <div
        style={{
          ...baseStyle,
          display: "flex",
          alignItems: "center",
        }}
        aria-hidden="true"
      >
        <div
          style={{
            whiteSpace: "pre",
            transform: `translateX(-${el.scrollLeft}px)`,
          }}
        >
          <span style={{ visibility: "hidden" }}>{textBeforeCursor}</span>
          <span className="text-muted-foreground/50">{ghostText}</span>
        </div>
      </div>
    );
  }

  return (
    <div style={baseStyle} aria-hidden="true">
      <div
        style={{
          whiteSpace: "pre-wrap",
          wordWrap: "break-word",
          overflowWrap: "break-word",
          transform: `translateY(-${el.scrollTop}px)`,
        }}
      >
        <span style={{ visibility: "hidden" }}>{textBeforeCursor}</span>
        <span className="text-muted-foreground/50">{ghostText}</span>
      </div>
    </div>
  );
}

export { ParameterGhostText };
