import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

import type { DebugSessionProfileIncompatibilityReason } from "./debugSessionProfileCompatibility";

const REASON_COPY: Record<DebugSessionProfileIncompatibilityReason, string> = {
  pbs_no_profile:
    "The visible debugger browser doesn't have a saved profile, but this login block's credential does. Continuing will run the login in the visible browser without the saved cookies — you may need to log in manually.",
  pbs_different_profile:
    "The visible debugger browser is running a different saved profile than this login block's credential. Continuing will run the login in the visible browser without the credential's saved cookies — you may need to log in manually.",
};

type Props = {
  open: boolean;
  reason: DebugSessionProfileIncompatibilityReason | null;
  onContinue: () => void;
  onCancel: () => void;
};

function DebugSessionProfileIncompatibleDialog({
  open,
  reason,
  onContinue,
  onCancel,
}: Props) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel();
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Profile mismatch with the debugger browser</DialogTitle>
          <DialogDescription>
            {reason ? REASON_COPY[reason] : null}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={onContinue}>Continue</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DebugSessionProfileIncompatibleDialog };
