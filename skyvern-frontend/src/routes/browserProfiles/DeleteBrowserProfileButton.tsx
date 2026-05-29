import { ReloadIcon, TrashIcon } from "@radix-ui/react-icons";

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

import { useDeleteBrowserProfileMutation } from "./hooks/useBrowserProfileMutations";

type Props = {
  profile: BrowserProfileApiResponse;
  onDeleted?: () => void;
};

function DeleteBrowserProfileButton({ profile, onDeleted }: Props) {
  const deleteMutation = useDeleteBrowserProfileMutation();

  return (
    <Dialog>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>
              <Button
                size="icon"
                variant="outline"
                aria-label="Delete browser profile"
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
          <DialogTitle>Are you sure?</DialogTitle>
        </DialogHeader>
        <div className="text-sm text-neutral-600 dark:text-slate-400">
          The browser profile{" "}
          <span className="font-bold text-primary">{profile.name}</span> will be
          deleted. Agents referencing this profile will no longer find it.
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button
            variant="destructive"
            onClick={async () => {
              await deleteMutation.mutateAsync(profile.browser_profile_id);
              onDeleted?.();
            }}
            disabled={deleteMutation.isPending}
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
