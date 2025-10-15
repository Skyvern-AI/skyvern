import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea, ScrollAreaViewport } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { Button } from "@/components/ui/button";
import { EyeClosedIcon, EyeOpenIcon } from "@radix-ui/react-icons";
import { useState } from "react";

type ParameterItem = {
  id: string;
  key: string;
  description?: string | null;
  type?: string | null;
  value?: string | null; // safe display value only; never raw secrets
};

type Props = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title?: string;
  sectionLabel?: string;
  items: Array<ParameterItem>;
};

export function ParametersDialogBase({
  open,
  onOpenChange,
  title = "Parameters",
  sectionLabel = "Parameters",
  items,
}: Props) {
  const [revealedIds, setRevealedIds] = useState<Set<string>>(new Set());

  function toggleReveal(id: string) {
    setRevealedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function renderRow(item: ParameterItem) {
    const revealed =
      item.value !== undefined &&
      item.value !== null &&
      item.value !== "" &&
      revealedIds.has(item.id);
    const isRevealable =
      item.value !== undefined && item.value !== null && item.value !== "";
    return (
      <div key={item.id} className="rounded-md border p-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <div className="break-all font-mono text-sm">{item.key}</div>
              {item.description ? (
                <div className="text-xs text-slate-400">
                  — {item.description}
                </div>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            {item.type ? <Badge variant="secondary">{item.type}</Badge> : null}
            {isRevealable ? (
              <Button
                size="sm"
                variant="outline"
                onClick={() => toggleReveal(item.id)}
                title={revealed ? "Hide value" : "Show value"}
              >
                {revealed ? (
                  <EyeClosedIcon className="h-4 w-4" />
                ) : (
                  <EyeOpenIcon className="h-4 w-4" />
                )}
              </Button>
            ) : null}
          </div>
        </div>
        {isRevealable ? (
          <div className="mt-2">
            <div className="rounded bg-slate-elevation2 p-2 font-mono text-xs">
              {revealed ? item.value : "••••••"}
            </div>
          </div>
        ) : null}
      </div>
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        {items.length === 0 ? (
          <div className="text-sm text-slate-400">No parameters.</div>
        ) : (
          <div className="space-y-3">
            <Label className="text-xs">{sectionLabel}</Label>
            <ScrollArea>
              <ScrollAreaViewport className="max-h-[420px]">
                <div className="space-y-3">
                  {items.map((it) => renderRow(it))}
                </div>
              </ScrollAreaViewport>
            </ScrollArea>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
