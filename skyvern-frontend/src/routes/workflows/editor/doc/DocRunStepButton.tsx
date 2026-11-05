import { PlayIcon, StopIcon } from "@radix-ui/react-icons";

import { cn } from "@/util/utils";
import { useBlockPlayRegistry } from "@/store/BlockPlayRegistryStore";
import type { WorkflowBlockNode } from "../nodes";
import { NodeHeader } from "../nodes/components/NodeHeader";
import { docBlockType, docStepCanRun } from "./docStepRegistry";

/** Runs one step through the canvas block's own run flow (gates, params, save-first). */
export function DocRunStepButton({
  node,
  disabled = false,
}: {
  node: WorkflowBlockNode;
  disabled?: boolean;
}) {
  const nodeId = node.id;
  const label = node.data.label;
  const blockType = docBlockType(node);
  const handle = useBlockPlayRegistry((s) => s.handles[nodeId]);
  if (!blockType || !docStepCanRun(node)) return null;
  // The canvas does not render this step (inactive branch, collapsed loop), so no
  // header registered a handle: host one here, without unhiding the node.
  const needsHost = !handle || handle.source === "doc";
  const host = needsHost ? (
    <NodeHeader
      headless
      blockLabel={label}
      editable
      nodeId={nodeId}
      totpIdentifier={
        "totpIdentifier" in node.data
          ? (node.data.totpIdentifier as string | null)
          : null
      }
      totpUrl={
        "totpVerificationUrl" in node.data
          ? (node.data.totpVerificationUrl as string | null)
          : null
      }
      type={blockType}
    />
  ) : null;
  if (!handle) return host;
  const { playing, inert, stopping } = handle;
  const name = playing ? `Stop ${label}` : `Run ${label}`;
  return (
    <>
      {host}
      <button
        type="button"
        aria-label={name}
        title={playing ? "Stop this step" : "Run this step"}
        disabled={disabled || (playing ? stopping : inert)}
        onClick={() => (playing ? handle.stop() : handle.play())}
        className={cn(
          "inline-flex items-center rounded-[5px] px-1 py-0.5 text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40",
          !playing &&
            "opacity-0 group-focus-within/line:opacity-100 group-hover/line:opacity-100 disabled:group-hover/line:opacity-40 [@media(hover:none)]:opacity-100",
        )}
      >
        {playing ? (
          <StopIcon className="size-[15px]" />
        ) : (
          <PlayIcon className="size-[15px]" />
        )}
      </button>
    </>
  );
}
