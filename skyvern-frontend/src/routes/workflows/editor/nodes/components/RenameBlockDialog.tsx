import { useState, useEffect, useRef } from "react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import {
  validateBlockLabel,
  sanitizeBlockLabel,
} from "@/routes/workflows/editor/blockLabelValidation";

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentLabel: string;
  existingLabels: string[];
  onRename: (newLabel: string) => void;
};

function RenameBlockDialog({
  open,
  onOpenChange,
  currentLabel,
  existingLabels,
  onRename,
}: Props) {
  const [value, setValue] = useState(currentLabel);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset state when dialog opens
  useEffect(() => {
    if (open) {
      setValue(currentLabel);
      setError(null);
      // Focus and select input after dialog opens
      setTimeout(() => {
        inputRef.current?.focus();
        inputRef.current?.select();
      }, 0);
    }
  }, [open, currentLabel]);

  const handleValueChange = (newValue: string) => {
    setValue(newValue);
    // Clear error while typing
    if (error) {
      setError(null);
    }
  };

  const handleConfirm = () => {
    const sanitized = sanitizeBlockLabel(value);

    // Validate
    const validationError = validateBlockLabel(sanitized, {
      existingLabels,
      currentLabel,
    });

    if (validationError) {
      setError(validationError);
      return;
    }

    // Don't save if unchanged
    if (sanitized === currentLabel) {
      onOpenChange(false);
      return;
    }

    onRename(sanitized);
    onOpenChange(false);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") {
      e.preventDefault();
      handleConfirm();
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-[425px]">
        <DialogHeader>
          <DialogTitle>Rename Block</DialogTitle>
          <DialogDescription>
            Give this block a descriptive name to make your workflow easier to
            understand.
          </DialogDescription>
        </DialogHeader>
        <div className="grid gap-4 py-4">
          <div className="grid gap-2">
            <Label htmlFor="block-name">Block Name</Label>
            <Input
              ref={inputRef}
              id="block-name"
              value={value}
              onChange={(e) => handleValueChange(e.target.value)}
              onKeyDown={handleKeyDown}
              placeholder="e.g., extract_pricing"
              className={error ? "border-destructive" : ""}
              aria-invalid={!!error}
              aria-describedby={error ? "block-name-error" : undefined}
            />
            {error && (
              <p
                id="block-name-error"
                className="text-sm font-medium text-destructive"
              >
                {error}
              </p>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Tip: Use descriptive names like <code>login_step</code> or{" "}
            <code>extract_data</code>
          </p>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button onClick={handleConfirm}>Rename</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { RenameBlockDialog };
