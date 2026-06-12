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
};

export function ClearCredentialDialog({
  label,
  title,
  description,
  disabled,
  isPending,
  onConfirm,
}: Props) {
  const [open, setOpen] = useState(false);

  const handleConfirm = () => {
    onConfirm();
    setOpen(false);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button type="button" variant="destructive" disabled={disabled}>
          {label}
        </Button>
      </DialogTrigger>
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
