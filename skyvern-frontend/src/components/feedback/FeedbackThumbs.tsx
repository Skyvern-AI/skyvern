import { useEffect, useState } from "react";

import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/util/utils";

export type FeedbackRating = "up" | "down";

export interface FeedbackRateOptions {
  needsSupport: boolean;
}

interface FeedbackThumbsProps {
  rating: FeedbackRating | null;
  // A reason already saved with a thumbs down; prefills the box if the user rates down again.
  reason?: string | null;
  // Called with null to clear a rating. A rejected promise keeps the previous state and shows a retry hint.
  onRate: (
    rating: FeedbackRating | null,
    reason?: string,
    options?: FeedbackRateOptions,
  ) => Promise<void> | void;
  prompt?: string;
  // Hidden until the enclosing `group/turn` is hovered or the control has focus, except
  // while a save is in flight, the reason box is open, or a status is showing.
  subtle?: boolean;
  // When set, the reason panel also offers a checkbox that flags the rating for human follow-up.
  supportOption?: { label: string };
  // "report" replaces the thumbs pair with a single thumbs-down action for runs that already failed.
  variant?: "thumbs" | "report";
  reportLabel?: string;
  className?: string;
}

function ThumbGlyph({ down }: { down?: boolean }) {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 16 16"
      className={cn("h-3.5 w-3.5", down && "rotate-180")}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M5 7.5v6H2.5v-6H5zm0 0 2.8-5.2a1.3 1.3 0 0 1 2.4.7L9.8 6h2.9a1.3 1.3 0 0 1 1.3 1.5l-.9 5a1.3 1.3 0 0 1-1.3 1H5" />
    </svg>
  );
}

const STATUS_FLASH_MS = 2500;
// Matches Tailwind's duration-150 so the text and the control fade out as one.
const STATUS_FADE_MS = 150;

export function FeedbackThumbs({
  rating,
  reason: savedReason,
  onRate,
  prompt,
  subtle = false,
  supportOption,
  variant = "thumbs",
  reportLabel = "Report this failure",
  className,
}: FeedbackThumbsProps) {
  const [pending, setPending] = useState(false);
  const [failed, setFailed] = useState(false);
  const [reasonOpen, setReasonOpen] = useState(false);
  const [reason, setReason] = useState(savedReason ?? "");
  const [flash, setFlash] = useState<string | null>(null);
  const [needsSupport, setNeedsSupport] = useState(false);

  useEffect(() => {
    if (rating !== "down") {
      setReasonOpen(false);
    }
  }, [rating]);

  useEffect(() => {
    if (!flash) {
      return;
    }
    const timer = setTimeout(() => setFlash(null), STATUS_FLASH_MS);
    return () => clearTimeout(timer);
  }, [flash]);

  const submit = async (next: FeedbackRating | null, text?: string) => {
    setPending(true);
    setFailed(false);
    try {
      if (supportOption && text !== undefined) {
        await onRate(next, text, { needsSupport });
      } else {
        await onRate(next, text);
      }
      if (next === "down" && text === undefined) {
        setReasonOpen(true);
        setFlash(null);
      } else if (next === null) {
        setFlash(null);
      } else {
        setReasonOpen(false);
        setFlash(text ? "Thanks, that helps." : "Thanks, noted.");
      }
    } catch {
      setFailed(true);
    } finally {
      setPending(false);
    }
  };

  const status = failed
    ? "Couldn't save. Try again."
    : (flash ??
      (reasonOpen && rating === "down"
        ? variant === "report"
          ? "Reported."
          : "Sorry about that."
        : rating === null
          ? variant === "report"
            ? null
            : (prompt ?? null)
          : variant === "report" && rating === "down"
            ? "Reported."
            : null));

  const hidden = subtle && !pending && !reasonOpen && status === null;

  // The last status stays mounted while it fades, instead of vanishing a beat before the thumbs.
  const [shownStatus, setShownStatus] = useState<string | null>(status);
  useEffect(() => {
    if (status !== null) {
      setShownStatus(status);
      return;
    }
    const timer = setTimeout(() => setShownStatus(null), STATUS_FADE_MS);
    return () => clearTimeout(timer);
  }, [status]);

  return (
    <div
      className={cn(
        "flex flex-col gap-2 text-xs transition-opacity duration-150",
        hidden &&
          "opacity-0 focus-within:opacity-100 group-hover/turn:opacity-100",
        className,
      )}
      data-testid="feedback-thumbs"
      data-subtle={hidden || undefined}
    >
      <div className="flex items-center gap-2">
        {shownStatus ? (
          <span
            className={cn(
              "text-muted-foreground transition-opacity duration-150",
              failed && "text-destructive",
              status === null && "opacity-0",
            )}
            role="status"
          >
            {shownStatus}
          </span>
        ) : null}
        {variant === "report" ? (
          rating === "down" ? (
            <button
              type="button"
              disabled={pending}
              onClick={() => submit(null)}
              className="text-muted-foreground underline-offset-2 hover:text-foreground hover:underline disabled:opacity-60"
            >
              Undo
            </button>
          ) : (
            <button
              type="button"
              disabled={pending}
              onClick={() => submit("down")}
              className="inline-flex h-7 items-center gap-1.5 rounded-md border border-white/15 px-2.5 text-xs text-foreground transition-colors hover:bg-accent disabled:opacity-60"
            >
              <ThumbGlyph down />
              {reportLabel}
            </button>
          )
        ) : (
          <>
            <button
              type="button"
              aria-label="Thumbs up"
              aria-pressed={rating === "up"}
              disabled={pending}
              onClick={() => submit(rating === "up" ? null : "up")}
              className={cn(
                "inline-flex h-6 w-6 items-center justify-center rounded-md border border-white/10 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-60",
                rating === "up" &&
                  "border-emerald-500/60 bg-emerald-950/60 text-emerald-200",
              )}
            >
              <ThumbGlyph />
            </button>
            <button
              type="button"
              aria-label="Thumbs down"
              aria-pressed={rating === "down"}
              disabled={pending}
              onClick={() => submit(rating === "down" ? null : "down")}
              className={cn(
                "inline-flex h-6 w-6 items-center justify-center rounded-md border border-white/10 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground disabled:opacity-60",
                rating === "down" &&
                  "border-orange-500/60 bg-orange-950/60 text-orange-200",
              )}
            >
              <ThumbGlyph down />
            </button>
          </>
        )}
      </div>
      {reasonOpen && rating === "down" ? (
        <div className="flex flex-col gap-2">
          <Textarea
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            placeholder="What was missing or wrong? (optional)"
            aria-label="Feedback reason"
            maxLength={2000}
            className="min-h-[56px] bg-slate-elevation3 text-xs"
          />
          {supportOption ? (
            <label className="flex items-center gap-2 text-xs text-foreground">
              <Checkbox
                checked={needsSupport}
                onCheckedChange={(checked) => setNeedsSupport(checked === true)}
              />
              {supportOption.label}
            </label>
          ) : null}
          <div className="flex gap-2">
            <button
              type="button"
              disabled={pending}
              onClick={() => submit("down", reason.trim())}
              className="rounded-md border border-white/15 px-2.5 py-1 text-xs text-foreground hover:bg-accent disabled:opacity-60"
            >
              Send
            </button>
            <button
              type="button"
              onClick={() => setReasonOpen(false)}
              className="rounded-md px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
            >
              Skip
            </button>
          </div>
        </div>
      ) : null}
    </div>
  );
}
