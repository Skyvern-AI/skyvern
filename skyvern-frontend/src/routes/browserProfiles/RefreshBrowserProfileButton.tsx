import { ReloadIcon, UpdateIcon } from "@radix-ui/react-icons";
import { useState } from "react";

import { BrowserProfileApiResponse } from "@/api/types";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
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
import { useCreateBrowserSessionMutation } from "@/routes/browserSessions/hooks/useCreateBrowserSessionMutation";

import { BrowserProfileUsageList } from "./BrowserProfileUsageList";
import { useBrowserProfileUsageQuery } from "./hooks/useBrowserProfileUsageQuery";

type Props = {
  profile: BrowserProfileApiResponse;
  // When set, render a labeled button (detail page); otherwise an icon button (list row).
  label?: string;
};

function RefreshBrowserProfileButton({ profile, label }: Props) {
  const [open, setOpen] = useState(false);
  const createSession = useCreateBrowserSessionMutation();
  const { data: usage, isLoading } = useBrowserProfileUsageQuery(
    profile.browser_profile_id,
    { enabled: open },
  );

  const disabled = Boolean(profile.deleted_at);

  const handleConfirm = () => {
    // Navigation to the live session view happens in the mutation's onSuccess. Launch from the profile's
    // own proxy identity so Refresh doesn't change the egress IP and re-trigger the very login challenge
    // the user is repairing.
    createSession.mutate({
      browserProfileId: profile.browser_profile_id,
      proxyLocation: profile.proxy_location ?? null,
      proxySessionId: profile.proxy_session_id ?? null,
      timeout: null,
    });
  };

  const trigger = label ? (
    <Button variant="secondary" disabled={disabled}>
      <UpdateIcon className="mr-2 size-4" />
      {label}
    </Button>
  ) : (
    <Button
      size="icon"
      variant="ghost"
      aria-label="Refresh browser profile"
      disabled={disabled}
      className="text-muted-foreground hover:text-foreground"
    >
      <UpdateIcon className="h-4 w-4" />
    </Button>
  );

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <DialogTrigger asChild>{trigger}</DialogTrigger>
          </TooltipTrigger>
          <TooltipContent>Refresh browser profile</TooltipContent>
        </Tooltip>
      </TooltipProvider>
      <DialogContent onCloseAutoFocus={(e) => e.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Open a live browser with this profile</DialogTitle>
          <DialogDescription>
            Skyvern opens a browser signed in with{" "}
            <span className="font-medium text-primary">{profile.name}</span>.
            Sign in again or clear the challenge, then close the session — the
            browser you leave behind is saved back to this same profile.
          </DialogDescription>
        </DialogHeader>
        <BrowserProfileUsageList usage={usage} isLoading={open && isLoading} />
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="secondary">Cancel</Button>
          </DialogClose>
          <Button onClick={handleConfirm} disabled={createSession.isPending}>
            {createSession.isPending && (
              <ReloadIcon className="mr-2 size-4 animate-spin" />
            )}
            Open browser session
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export { RefreshBrowserProfileButton };
