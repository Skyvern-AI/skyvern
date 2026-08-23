import { Share1Icon } from "@radix-ui/react-icons";
import { useSearchParams } from "react-router-dom";

import { readSelectedBlockLabel } from "./selectedBlockLabel";

/**
 * Composer chip mirroring the studio canvas selection, so the user can see the
 * context the copilot receives with their message. Its own component so URL
 * changes re-render this chip, not the whole chat.
 */
export function SelectedBlockChip() {
  const [searchParams] = useSearchParams();
  const label = readSelectedBlockLabel(searchParams.toString());
  if (!label) {
    return null;
  }
  return (
    <div
      title="Copilot can see this canvas selection"
      className="mb-2 inline-flex max-w-full items-center gap-1.5 rounded-full bg-slate-400/[0.12] px-2.5 py-0.5 text-xs text-muted-foreground"
    >
      <Share1Icon className="h-3 w-3 shrink-0" aria-hidden />
      <span className="shrink-0">Selected block:</span>
      <span className="truncate font-medium text-foreground">{label}</span>
    </div>
  );
}
