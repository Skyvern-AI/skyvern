import { useEditorOnboardingTour } from "@/hooks/useEditorOnboardingTour";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import "@/util/onboarding/product-tour.css";

function ProductTour() {
  const { showExitDialog, onExitConfirm, onExitCancel } =
    useEditorOnboardingTour();

  return (
    <Dialog
      open={showExitDialog}
      onOpenChange={(open) => {
        if (!open) onExitCancel();
      }}
    >
      <DialogContent
        className="skyvern-tour-exit"
        onPointerDownOutside={(e) => e.preventDefault()}
        onEscapeKeyDown={(e) => e.preventDefault()}
      >
        <DialogHeader>
          <DialogTitle>End the tour?</DialogTitle>
          <DialogDescription>
            You can restart anytime with Shift+? or from the overflow menu.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="secondary" onClick={onExitCancel}>
            Continue tour
          </Button>
          <Button variant="default" onClick={onExitConfirm}>
            End tour
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { ProductTour };
