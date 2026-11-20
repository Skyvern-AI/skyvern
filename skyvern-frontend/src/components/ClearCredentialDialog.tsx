import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

type Props = {
  label: string;
  title: string;
  description: string;
  disabled: boolean;
  isPending: boolean;
  onConfirm: () => void;
  // When set, the dialog is opened from elsewhere (such as a menu item) and renders no trigger button.
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
};

export function ClearCredentialDialog({
  label,
  title,
  description,
  disabled,
  isPending,
  onConfirm,
  open: controlledOpen,
  onOpenChange,
}: Props) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false);
  const controlled = controlledOpen !== undefined;
  const open = controlled ? controlledOpen : uncontrolledOpen;
  const setOpen = (next: boolean) => {
    if (controlled) {
      onOpenChange?.(next);
    } else {
      setUncontrolledOpen(next);
    }
  };

  const handleConfirm = () => {
    onConfirm();
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      {controlled ? null : (
        <DialogTrigger asChild>
          <Button type="button" variant="destructive" disabled={disabled}>
            {label}
          </Button>
        </DialogTrigger>
      )}
      {/* z-[60]: this dialog can be nested inside another Dialog (cloud
          integrations tile); all overlays/contents share z-50, so paint order
          otherwise depends on portal DOM order alone. */}
      <DialogContent
        className="z-[60]"
        onCloseAutoFocus={(e) => e.preventDefault()}
      >
        <DialogTitle>{title}</DialogTitle>
        <DialogDescription>{description}</DialogDescription>
        <DialogFooter>
          <Button
            type="button"
            variant="secondary"
            disabled={isPending}
            onClick={() => setOpen(false)}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="destructive"
            onClick={handleConfirm}
            disabled={disabled}
          >
            {isPending ? "Clearing..." : label}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
