import { TrashIcon } from "@radix-ui/react-icons";
import { useState } from "react";

import { BrowserProfileApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

import { BrowserProfileUsageList } from "./BrowserProfileUsageList";
import { deleteWarning, getBrowserProfileRole } from "./browserProfileRole";
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

  // A remembered browser (workflow memory) or a credential login is re-created on the
  // next run/sign-in, so those deletes are soft. A plain saved profile is gone for good.
  const isSoftDelete = getBrowserProfileRole(profile, usage) !== "plain";

  return (
    <>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              size="icon"
              variant="ghost"
              aria-label="Delete browser profile"
              className="text-muted-foreground hover:text-destructive"
              onClick={() => setOpen(true)}
            >
              <TrashIcon className="h-4 w-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent>Delete Browser Profile</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={
          <>
            Delete{" "}
            <span className="font-bold text-primary">{profile.name}</span>?
          </>
        }
        description={<p>{deleteWarning(profile, usage)}</p>}
        reversible={isSoftDelete}
        isPending={deleteMutation.isPending}
        // Hold the destructive action until the used-by list has loaded so a user can't confirm
        // before seeing what depends on the profile. A usage error re-enables it (warn, never block).
        confirmDisabled={isLoading}
        onConfirm={async () => {
          await deleteMutation.mutateAsync(profile.browser_profile_id);
          setOpen(false);
          onDeleted?.();
        }}
      >
        <BrowserProfileUsageList usage={usage} isLoading={open && isLoading} />
      </ConfirmDialog>
    </>
  );
}

export { DeleteBrowserProfileButton };
