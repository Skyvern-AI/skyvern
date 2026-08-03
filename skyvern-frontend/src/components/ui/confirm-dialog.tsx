import * as React from "react";
import { ReloadIcon } from "@radix-ui/react-icons";

import { Button, type ButtonProps } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const BULK_TYPED_CONFIRMATION_THRESHOLD = 10;
const DEFAULT_CONFIRMATION_PHRASE = "delete";
const DEFAULT_REVERSIBILITY_NOTE = "This can't be undone.";

type ConfirmDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** What the user is confirming. e.g. "Delete 3 schedules?" */
  title: React.ReactNode;
  /**
   * What happens, and to how many items. Surfaces pass this copy; the dialog
   * owns the structure and the standardized reversibility line below it.
   */
  description?: React.ReactNode;
  /** Extra content rendered below the description (usage lists, radio choices, etc). */
  children?: React.ReactNode;
  /**
   * Omits the standardized "This can't be undone." line for actions that can be
   * undone, or that spell out reversibility in the description.
   */
  reversible?: boolean;
  /** Overrides the standardized reversibility line (e.g. stronger "PERMANENTLY deleted" wording). */
  reversibilityNote?: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  confirmVariant?: ButtonProps["variant"];
  /** Shows a spinner and blocks interaction while the action runs. */
  isPending?: boolean;
  /**
   * Extra gating on the confirm button, independent of pending/typed state.
   * e.g. holding Delete until a usage lookup resolves.
   */
  confirmDisabled?: boolean;
  onConfirm: () => void;
  /**
   * Require the user to type {@link confirmationPhrase} before confirming.
   * Auto-enabled when {@link itemCount} reaches the bulk threshold.
   */
  requireTypedConfirmation?: boolean;
  confirmationPhrase?: string;
  /** Number of items affected; when >= threshold, typed confirmation is required. */
  itemCount?: number;
  typedConfirmationThreshold?: number;
  /** Extra classes for the dialog content (e.g. `z-[60]` when nested in another dialog). */
  contentClassName?: string;
};

function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
  reversible = false,
  reversibilityNote,
  confirmLabel = "Delete",
  cancelLabel = "Cancel",
  confirmVariant = "destructive",
  isPending = false,
  confirmDisabled = false,
  onConfirm,
  requireTypedConfirmation = false,
  confirmationPhrase = DEFAULT_CONFIRMATION_PHRASE,
  itemCount,
  typedConfirmationThreshold = BULK_TYPED_CONFIRMATION_THRESHOLD,
  contentClassName,
}: ConfirmDialogProps) {
  const inputId = React.useId();
  const [typed, setTyped] = React.useState("");

  const needsTypedConfirmation =
    requireTypedConfirmation ||
    (itemCount !== undefined && itemCount >= typedConfirmationThreshold);

  React.useEffect(() => {
    if (!open) {
      setTyped("");
    }
  }, [open]);

  const typedMatches =
    typed.trim().toLowerCase() === confirmationPhrase.trim().toLowerCase();
  const confirmBlocked =
    confirmDisabled || isPending || (needsTypedConfirmation && !typedMatches);

  const handleConfirm = () => {
    if (confirmBlocked) {
      return;
    }
    onConfirm();
  };

  const showReversibilityNote = !reversible;
  const hasDescriptionBlock = Boolean(description) || showReversibilityNote;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // Block dismissal while the action is in flight so a mid-delete close
        // can't strand the caller's pending state.
        if (isPending) {
          return;
        }
        onOpenChange(next);
      }}
    >
      <DialogContent
        className={contentClassName}
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          {hasDescriptionBlock ? (
            <DialogDescription asChild>
              <div className="space-y-3">
                {typeof description === "string" ? (
                  <p>{description}</p>
                ) : (
                  description
                )}
                {showReversibilityNote ? (
                  <p>{reversibilityNote ?? DEFAULT_REVERSIBILITY_NOTE}</p>
                ) : null}
              </div>
            </DialogDescription>
          ) : null}
        </DialogHeader>
        {children}
        {needsTypedConfirmation ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor={inputId}>
              Type{" "}
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                {confirmationPhrase}
              </code>{" "}
              to confirm
            </Label>
            <Input
              id={inputId}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") {
                  event.preventDefault();
                  handleConfirm();
                }
              }}
              autoComplete="off"
              autoFocus
              disabled={isPending}
            />
          </div>
        ) : null}
        <DialogFooter>
          <Button
            variant="secondary"
            disabled={isPending}
            onClick={() => onOpenChange(false)}
          >
            {cancelLabel}
          </Button>
          <Button
            variant={confirmVariant}
            disabled={confirmBlocked}
            onClick={handleConfirm}
          >
            {isPending ? (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            ) : null}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { ConfirmDialog, BULK_TYPED_CONFIRMATION_THRESHOLD };
export type { ConfirmDialogProps };
