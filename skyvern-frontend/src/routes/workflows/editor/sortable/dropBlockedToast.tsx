import { toast } from "@/components/ui/use-toast";

/**
 * Unified drop-blocked toast.
 *
 * The block-reorder pipeline has four independent drop-block paths — forward-
 * reference violations, the finally-block pin, recording mode, and
 * cross-scope drops — each with its own fix. This module consolidates them
 * behind one toast so users always get the same affordance (same position,
 * same voice, same variant) and the message names the offending constraint
 * specifically enough that the next action is obvious.
 *
 * The formatter is deliberately pure so {@link formatDropBlockedToast} can
 * be unit-tested without mounting the toast viewport. {@link
 * showDropBlockedToast} is the production entry point used by FlowRenderer.
 *
 * Accessibility: the toast is fired with `type: "background"` so Radix's
 * `@radix-ui/react-toast` renders it with `aria-live="polite"` (per the
 * ticket AC). The polite channel avoids interrupting the screen-reader
 * user mid-sentence while still announcing why their drop was refused.
 */

export type DropBlockedReason =
  | {
      kind: "forward-reference";
      /** Human-readable label of the block that was dragged. */
      movedBlockLabel: string;
      /** Labels of the blocks that would forward-reference the moved block. */
      referrerLabels: string[];
    }
  | {
      kind: "finally-pin";
      /** Label of the finally block that must stay at the tail. */
      finallyBlockLabel: string;
    }
  | {
      kind: "drag-mode";
    }
  | {
      kind: "cross-scope";
      /** Human-readable label of the block that was dragged. */
      movedBlockLabel: string;
    }
  | {
      kind: "chain-mismatch";
    };

export type DropBlockedToastContent = {
  title: string;
  /** Primary sentence explaining the violated constraint. */
  description: string;
  /**
   * Optional list of extra lines — used by `forward-reference` to name each
   * referring block. Other reasons leave this empty.
   */
  details: string[];
};

/**
 * Pure formatter: map a {@link DropBlockedReason} to the toast copy. Kept
 * string-only (no React nodes) so the result is trivially snapshot-able in
 * unit tests. Callers that need richer markup compose it from these fields.
 */
export function formatDropBlockedToast(
  reason: DropBlockedReason,
): DropBlockedToastContent {
  switch (reason.kind) {
    case "forward-reference": {
      // Name the moved block in the description so the user can map the
      // toast back to the block they just dragged — the referrers list
      // below tells them which blocks would break.
      const uniqueReferrers = Array.from(new Set(reason.referrerLabels));
      return {
        title: "Can't reorder: would create a forward reference",
        description: `"${reason.movedBlockLabel}" is referenced by blocks that would run before it after this drop.`,
        details: uniqueReferrers,
      };
    }
    case "finally-pin": {
      return {
        title: "Can't reorder: finally block must run last",
        description: `"${reason.finallyBlockLabel}" must remain the last block so it runs on any outcome.`,
        details: [],
      };
    }
    case "drag-mode": {
      return {
        title: "Can't reorder: recording is active",
        description: "Stop recording to reorder blocks.",
        details: [],
      };
    }
    case "cross-scope": {
      return {
        title: "Can't reorder: drop target is outside this group",
        description: `"${reason.movedBlockLabel}" can only be reordered among its own siblings — loop and conditional-branch blocks stay inside their container.`,
        details: [],
      };
    }
    case "chain-mismatch": {
      return {
        title: "Can't reorder: workflow chain is out of sync",
        description:
          "The block chain has a missing or stale edge — refresh the page and try again. If the problem persists, the workflow may need to be re-saved.",
        details: [],
      };
    }
  }
}

/**
 * Fire the unified drop-blocked toast. `type: "background"` routes the
 * announcement through Radix's polite aria-live region so screen readers
 * get the reason without interrupting other speech.
 */
export function showDropBlockedToast(reason: DropBlockedReason): void {
  const { title, description, details } = formatDropBlockedToast(reason);
  toast({
    variant: "destructive",
    // Radix Toast maps `type: "background"` to role="status" +
    // aria-live="polite". The default ("foreground") would announce as
    // assertive, which is too interruptive for a drop-refusal — this is
    // informational, not an emergency.
    type: "background",
    title,
    description:
      details.length > 0 ? (
        <div className="space-y-1">
          <p>{description}</p>
          {details.map((line) => (
            <p key={line} className="font-medium">
              {line}
            </p>
          ))}
        </div>
      ) : (
        description
      ),
  });
}
