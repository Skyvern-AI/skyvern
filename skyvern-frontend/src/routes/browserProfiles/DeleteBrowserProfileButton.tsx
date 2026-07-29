import { ReloadIcon, TrashIcon } from "@radix-ui/react-icons";
import { useState } from "react";

import { BrowserProfileApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { BrowserProfileUsageList } from "./BrowserProfileUsageList";
import { deleteWarning } from "./browserProfileRole";
import { useBrowserProfileUsageQuery } from "./hooks/useBrowserProfileUsageQuery";
import { useDeleteBrowserProfileMutation } from "./hooks/useBrowserProfileMutations";

type Props = {
  profile: BrowserProfileApiResponse;
  onDeleted?: () => void;
};

function DeleteBrowserProfileButton({ profile, onDeleted }: Props) {
  const [open, setOpen] = useState(false);
  const deleteMutation = useDeleteBrowserProfileMutation();
  const { data: usage, isLoading } = useBrowserProfileUsageQuery(
    profile.browser_profile_id,
    { enabled: open },
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>
              <Button
                size="icon"
                variant="ghost"
                aria-label="Delete browser profile"
                className="text-muted-foreground hover:text-destructive"
              >
                <TrashIcon className="h-4 w-4" />
              </Button>
            </DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>Delete Browser Profile</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>
            Delete{" "}
            <span className="font-bold text-primary">{profile.name}</span>?
          </DialogTitle>
        </DialogHeader>
        <div className="space-y-4 text-sm text-neutral-600 dark:text-slate-400">
          <p>{deleteWarning(profile, usage)}</p>
          <BrowserProfileUsageList
            usage={usage}
            isLoading={open && isLoading}
          />
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={async () => {
              await deleteMutation.mutateAsync(profile.browser_profile_id);
              setOpen(false);
              onDeleted?.();
            }}
            // Hold the destructive action until the used-by list has loaded so a user can't confirm
            // before seeing what depends on the profile. A usage error re-enables it (warn, never block).
            disabled={deleteMutation.isPending || isLoading}
          >
            {deleteMutation.isPending && (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            )}
            Delete
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { DeleteBrowserProfileButton };
