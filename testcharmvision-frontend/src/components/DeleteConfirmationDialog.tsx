import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

type AffectedBlock = {
  nodeId: string;
  label: string;
  hasParameterKeyReference: boolean;
  hasJinjaReference: boolean;
};

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  affectedBlocks: AffectedBlock[];
  onConfirm: () => void;
};

function DeleteConfirmationDialog({
  open,
  onOpenChange,
  title,
  description,
  affectedBlocks,
  onConfirm,
}: Props) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription asChild>
            <div className="space-y-3">
              <p>{description}</p>
              {affectedBlocks.length > 0 && (
                <div className="rounded-md border border-amber-500/50 bg-amber-500/10 p-3">
                  <p className="mb-2 font-medium text-amber-500">
                    The following blocks reference this item and will be
                    updated:
                  </p>
                  <ul className="list-inside list-disc space-y-1 text-sm text-slate-300">
                    {affectedBlocks.map((block) => (
                      <li key={block.nodeId}>
                        <span className="font-medium">{block.label}</span>
                        <span className="text-slate-400">
                          {" "}
                          (
                          {[
                            block.hasParameterKeyReference &&
                              "parameter selector",
                            block.hasJinjaReference && "text field",
                          ]
                            .filter(Boolean)
                            .join(", ")}
                          )
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={() => {
              onConfirm();
            }}
          >
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteConfirmationDialog };
export type { AffectedBlock };
